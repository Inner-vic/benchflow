"""Real reviewed template, synthetic frozen inputs; never import bench code."""
import ast
import copy
import json
from pathlib import Path
import unittest
from bench_gateway import candidate_adapter as adapter

from bench_gateway.candidate_adapter import (
    COMMON, CONTROL_HASHES, SO_TARGETS, TEMPLATE_HASHES, TEMPLATE_ID, digest, render,
)


def fixture():
    root = Path(__file__).resolve().parents[1] / "research" / ("template-" + TEMPLATE_ID)
    texts = {name: (root / name).read_text(encoding="utf-8") for name in TEMPLATE_HASHES}
    template = {"id": TEMPLATE_ID, "hashes": dict(TEMPLATE_HASHES),
                "job_text": texts["job.json"], "profile_text": texts["profile.json"],
                "job": json.loads(texts["job.json"]), "profile": json.loads(texts["profile.json"]),
                "adapter": texts["small_stream_adapter.py"],
                "driver": texts["sw0058_gate_probe_driver_reference.py"]}
    config = """replay:
  enable: OFF
bench_mode:
  enable: false
speed_gate:
  enable: true
perception_gate:
  enable: true
apa_gate:
  enable: true
gear_gate:
  enable: true
eval_dump:
  enable: OFF
  output_dir: /app/livc/data/factory-output
  scene_root: /app/livc/data/factory-scenes
  save_traj: true
  save_traj_xy: true
  save_vision: false
  gt_trajectory:
    enable: ON
"""
    owner = "/mnt/data/bench-gateway/users/u_test_owner"
    replacements = [{"target": name, "path": owner + "/dependencies/" + Path(name).name,
                     "sha256": digest(name + "current user bytes"), "kind": "dependency"}
                    for name in SO_TARGETS]
    replacements.append({"target": "etc/uroad/config/other.yaml", "path": owner + "/preparations/p1/other.yaml",
                         "sha256": digest("new user YAML"), "kind": "yaml"})
    manifest = {item["target"]: {"kind": "file", "sha256": item["sha256"]} for item in replacements}
    manifest[COMMON] = {"kind": "file", "sha256": digest(config)}
    manifest.update({name: {"kind": "file", "sha256": value} for name, value in CONTROL_HASHES.items()})
    candidate = {"owner_root": owner, "package": {"path": owner + "/packages/new.app.tar.gz", "sha256": digest("new package")},
                 "configuration": {"path": owner + "/preparations/p1/bindings/b1/common_param.yaml",
                                   "text": config, "sha256": digest(config)},
                 "original_configuration_text": config, "formal_manifest": manifest,
                 "replacements": replacements,
                 "controls": [{"target": name, "path": "/mnt/data/bench-gateway/shared/tools/" + name, "sha256": value}
                              for name, value in CONTROL_HASHES.items()],
                 "dataset": {"remote_path": "/mnt/data/shared-demo", "manifest": "/mnt/data/shared-demo/manifest.json",
                             "sha256": digest("new manifest"),
                             "camera_roots": ["/mnt/data/shared-demo/camera-s001", "/mnt/data/shared-demo/camera-s002"],
                             "prepared_scenes": [{"scene": "scene-one", "camera_frames": 21}, {"scene": "scene-two", "camera_frames": 34}]},
                 "yaml_plan": {"operations": []}, "yaml_preparation": {}}
    return template, candidate


def update_config(candidate, text, operation=None, whole_replacement=False):
    candidate["configuration"].update(text=text, sha256=digest(text))
    candidate["formal_manifest"][COMMON]["sha256"] = digest(text)
    if operation:
        candidate["yaml_plan"]["operations"].append(operation)
    if whole_replacement:
        candidate["replacements"].append({"target": COMMON, "path": candidate["configuration"]["path"],
                                          "sha256": digest(text), "kind": "yaml"})


def assignment(source, name):
    from bench_gateway.candidate_adapter import assignment_name
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and assignment_name(node.targets[0]) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(name)


class CandidateAdapterTests(unittest.TestCase):
    def test_real_template_render_uses_only_current_materials(self):
        template, candidate = fixture()
        before = copy.deepcopy((template, candidate))
        result = render(template, candidate, "new-own-package-smoke", "actor_owner", [2])
        self.assertEqual(before, (template, candidate))
        self.assertEqual(result["job"]["app_artifact"]["path"], candidate["package"]["path"])
        self.assertEqual(result["job"]["coordination"]["allowed_actors"], ["actor_owner"])
        self.assertEqual(result["job"]["replay_input"]["prepared_manifest"], candidate["dataset"]["manifest"])
        self.assertEqual(result["job"]["batch_stream"]["scenes"], [{"scene": "scene-two", "timestamped_jpeg_frames": "/mnt/data/shared-demo/camera-s002"}])
        self.assertEqual(result["identity"]["required_output_types"], ["pred_traj", "traj_xy"])
        self.assertFalse(result["identity"]["gate_verified"])
        self.assertEqual(result["identity"]["hardware_actions"], "NOT_RUN")
        for material in (template["job"]["app_artifact"], template["job"]["configuration_input"]):
            self.assertNotIn(material["path"], json.dumps(result))
            self.assertNotIn(material["sha256"], json.dumps(result))
        old_hashes = assignment(template["driver"], "FORMAL_BASELINE_HASHES")
        for old_hash in old_hashes.values():
            self.assertNotIn(old_hash, result["driver"])
            self.assertNotIn(old_hash, result["adapter"])
        for source, variable in ((result["driver"], "FORMAL_BASELINE_HASHES"), (result["adapter"], "case.FORMAL_BASELINE_HASHES")):
            actual = assignment(source, variable)
            self.assertEqual(list(actual), sorted(actual))
            self.assertEqual(actual["/app/livc/etc/uroad/config/other.yaml"], digest("new user YAML"))
            self.assertEqual(actual["/app/livc/" + COMMON], candidate["configuration"]["sha256"])
        self.assertNotIn("gt_trajectory_input", result["job"])
        self.assertNotIn("postprocess_input", result["job"])
        self.assertNotIn('startswith("/mnt/data/bench-eval/adapters/")', result["adapter"])
        self.assertIn("/mnt/data/shared-demo/camera-s002", result["adapter"])

    def test_full_selection_and_vision_output_come_from_current_config(self):
        template, candidate = fixture()
        update_config(candidate, candidate["configuration"]["text"].replace("save_vision: false", "save_vision: true"))
        result = render(template, candidate, "new-full", "actor")
        self.assertEqual(result["job"]["replay_input"]["scene_count"], 2)
        self.assertIn("len(entries) != 2", result["adapter"])
        self.assertEqual(result["identity"]["required_output_types"], ["pred_traj", "traj_xy", "pred_img"])

    def test_template_mutations_fail_even_if_caller_updates_hash(self):
        for key, filename in (("adapter", "small_stream_adapter.py"), ("driver", "sw0058_gate_probe_driver_reference.py")):
            with self.subTest(key=key):
                template, candidate = fixture()
                template[key] += "\n# different protocol\n"
                template["hashes"][filename] = digest(template[key])
                with self.assertRaisesRegex(ValueError, "source hash"):
                    render(template, candidate, "new", "actor")
        template, candidate = fixture()
        template["job"]["workflow"]["targets"] = ["different"]
        with self.assertRaisesRegex(ValueError, "object differs"):
            render(template, candidate, "new", "actor")

    def test_missing_or_changed_shared_controls_fail_before_render(self):
        for mutation in (lambda c: c.pop("controls"), lambda c: c["controls"].pop(),
                         lambda c: c["controls"][0].update(sha256=digest("bad tool"))):
            template, candidate = fixture()
            mutation(candidate)
            with self.assertRaises(ValueError):
                render(template, candidate, "new", "actor")

    def test_foreign_package_dependency_or_configuration_refused(self):
        for field in ("package", "configuration", "dependency"):
            template, candidate = fixture()
            item = candidate["replacements"][0] if field == "dependency" else candidate[field]
            item["path"] = "/mnt/data/bench-gateway/users/someone_else/asset"
            with self.assertRaisesRegex(ValueError, "outside this user's"):
                render(template, candidate, "new", "actor")

    def test_all_three_user_libraries_and_frozen_hashes_required(self):
        template, candidate = fixture()
        candidate["replacements"].pop(0)
        with self.assertRaisesRegex(ValueError, "camera, lidar and M100"):
            render(template, candidate, "new", "actor")
        template, candidate = fixture()
        candidate["formal_manifest"]["etc/uroad/config/other.yaml"]["sha256"] = digest("stale YAML")
        with self.assertRaisesRegex(ValueError, "differs from current"):
            render(template, candidate, "new", "actor")

    def test_explicit_forced_field_conflict_even_if_value_unchanged(self):
        template, candidate = fixture()
        candidate["yaml_plan"]["operations"] = [{"op": "set", "target": COMMON, "path": ["replay", "enable"]}]
        with self.assertRaisesRegex(ValueError, "conflicts.*replay.enable"):
            render(template, candidate, "new", "actor")

    def test_parent_mapping_conflict_and_whole_replacement_conflict(self):
        template, candidate = fixture()
        candidate["yaml_plan"]["operations"] = [{"operation": "set", "target": COMMON, "path": ["speed_gate"]}]
        with self.assertRaisesRegex(ValueError, "conflicts.*speed_gate.enable"):
            render(template, candidate, "new", "actor")
        template, candidate = fixture()
        text = candidate["configuration"]["text"].replace("output_dir: /app/livc/data/factory-output", "output_dir: /different/user-choice")
        update_config(candidate, text, {"op": "replace", "target": COMMON}, True)
        with self.assertRaisesRegex(ValueError, "conflicts.*eval_dump.output_dir"):
            render(template, candidate, "new", "actor")

    def test_allowed_yaml_is_preserved_without_mutating_noncommon_text(self):
        template, candidate = fixture()
        text = candidate["configuration"]["text"].replace("replay:\n  enable: OFF", "replay:\n  enable: ON")
        update_config(candidate, text, {"operation": "set", "target": COMMON, "path": ["replay", "enable"]}, True)
        result = render(template, candidate, "new", "actor")
        self.assertEqual(result["job"]["configuration_input"]["sha256"], digest(text))
        self.assertIn("etc/uroad/config/other.yaml", [r["target"] for r in result["identity"]["replacements"]])

    def test_whole_replacement_requires_original_and_current_bytes(self):
        template, candidate = fixture()
        update_config(candidate, candidate["configuration"]["text"], {"op": "replace", "target": COMMON}, True)
        candidate.pop("original_configuration_text")
        with self.assertRaisesRegex(ValueError, "original common YAML"):
            render(template, candidate, "new", "actor")
        template, candidate = fixture()
        candidate["configuration"]["text"] += "# changed after freeze\n"
        with self.assertRaisesRegex(ValueError, "frozen bytes"):
            render(template, candidate, "new", "actor")

    def test_missing_ambiguous_required_fields_and_disabled_output_fail(self):
        for before, after in (("  save_traj: true", "  save_traj: false"),
                              ("  save_traj_xy: true", "  save_traj_xy: [true]"),
                              ("  save_vision: false", ""),
                              ("replay:\n  enable: OFF", "replay:\n  enable: OFF\n  enable: ON")):
            template, candidate = fixture()
            update_config(candidate, candidate["configuration"]["text"].replace(before, after))
            with self.assertRaises(ValueError):
                render(template, candidate, "new", "actor")
        template, candidate = fixture()
        update_config(candidate, "\ufeff" + candidate["configuration"]["text"])
        with self.assertRaisesRegex(ValueError, "UTF-8 BOM"):
            render(template, candidate, "new", "actor")

    def test_bad_scene_identity_selection_and_paths_fail(self):
        for indices in ([], [0], [3], [2, 1], [1, 1], [True]):
            template, candidate = fixture()
            with self.assertRaises(ValueError):
                render(template, candidate, "new", "actor", indices)
        template, candidate = fixture()
        candidate["dataset"]["camera_roots"][0] = "/mnt/data/../other"
        with self.assertRaises(ValueError):
            render(template, candidate, "new", "actor")


class ProtocolNormalizationTests(unittest.TestCase):
    def old_schema(self):
        template,candidate=fixture()
        text=candidate['original_configuration_text'].replace('  gt_trajectory:\n    enable: ON\n','')
        raw,m100,_=next(iter(adapter.NORMALIZATION_INPUTS))
        candidate['package']['sha256']=raw
        candidate['package_source_sha256']=raw
        candidate['package_origin']='personal'
        for item in candidate['replacements']:
            if item['target']=='lib/liburoad_m100.so':
                item['sha256']=m100;candidate['formal_manifest'][item['target']]['sha256']=m100
        candidate['original_configuration_text']=text
        update_config(candidate,text)
        return template,candidate

    def normalize(self,candidate,**overrides):
        kwargs={'package_sha256':candidate['package']['sha256'],
                'package_source_sha256':candidate['package_source_sha256'],
                'package_origin':candidate['package_origin'],
                'm100_sha256':next(i['sha256'] for i in candidate['replacements'] if i['target']=='lib/liburoad_m100.so'),
                'template_id':TEMPLATE_ID,'user_operations':candidate['yaml_plan']['operations'],
                'replacements':candidate['replacements']}
        kwargs.update(overrides)
        return adapter.protocol_normalize_common(candidate['original_configuration_text'],candidate['configuration']['text'],**kwargs)

    def bind_proof(self,candidate):
        text,proof=self.normalize(candidate)
        update_config(candidate,text)
        candidate['protocol_normalization']=proof
        candidate['binding_identity']={'protocol_normalization_sha256':adapter.normalization_digest(proof)}
        candidate['replacements'].append({'kind':'protocol_normalization','target':COMMON,
            'path':candidate['configuration']['path'],'sha256':proof['after_sha256']})
        return proof

    def test_whitelisted_old_schema_adds_only_gt_off_and_renders_current_config(self):
        template,candidate=self.old_schema();before=candidate['original_configuration_text']
        proof=self.bind_proof(candidate)
        self.assertEqual(before.replace('eval_dump:\n','eval_dump:\n  gt_trajectory:\n    enable: OFF\n'),candidate['configuration']['text'])
        self.assertEqual(before,candidate['original_configuration_text'])
        self.assertEqual({adapter.GT_FIELD:False},proof['fields'])
        result=render(template,candidate,'current-schema-smoke','actor')
        self.assertEqual(proof['after_sha256'],result['job']['configuration_input']['sha256'])
        self.assertEqual(['pred_traj','traj_xy'],result['identity']['required_output_types'])

    def test_unknown_package_m100_template_do_not_gain_schema_exception(self):
        for change in ({'package_sha256':'a'*64,'package_source_sha256':'a'*64},
                       {'m100_sha256':'b'*64},{'template_id':'unknown'}):
            with self.subTest(change=change):
                template,candidate=self.old_schema();text,proof=self.normalize(candidate,**change)
                self.assertIsNone(proof);self.assertEqual(candidate['configuration']['text'],text)
                with self.assertRaisesRegex(ValueError,'gt_trajectory'):
                    render(template,candidate,'unreviewed','actor')

    def test_user_changes_ambiguous_mapping_and_other_missing_fields_fail_closed(self):
        for transform in (lambda s:s+'\neval_dump:\n  extra: true\n',
                          lambda s:s.replace('eval_dump:','"eval_dump":'),
                          lambda s:s.replace('eval_dump:','eval_dump: {}'),
                          lambda s:s.replace('eval_dump:\n','eval_dump:\n  gt_trajectory: {}\n'),
                          lambda s:s.replace('  save_traj: true','  save_traj: [true]'),
                          lambda s:s.replace('  save_traj_xy: true\n',''),
                          lambda s:s.replace('  scene_root: /app/livc/data/factory-scenes\n','')):
            _,candidate=self.old_schema();text=transform(candidate['original_configuration_text'])
            candidate['original_configuration_text']=text;update_config(candidate,text)
            with self.subTest(text=text),self.assertRaises(ValueError):self.normalize(candidate)
        _,candidate=self.old_schema()
        for kwargs in ({'user_operations':[{'target':COMMON,'operation':'set','path':'eval_dump.enable','value':True}]},
                       {'replacements':[{'target':COMMON,'kind':'yaml'}]}):
            with self.subTest(kwargs=kwargs),self.assertRaisesRegex(ValueError,'user common'):self.normalize(candidate,**kwargs)
        candidate['configuration']['text']+='# any prior modification\n'
        with self.assertRaisesRegex(ValueError,'unchanged'):self.normalize(candidate)

    def test_assembly_lineage_keeps_original_sha_but_raw_override_is_rejected(self):
        _,candidate=self.old_schema();raw=candidate['package']['sha256']
        candidate['package']['sha256']='e'*64
        with self.assertRaisesRegex(ValueError,'raw package'):self.normalize(candidate)
        candidate['package_origin']='assembly_catalog';text,proof=self.normalize(candidate)
        self.assertEqual(raw,proof['package_sha256']);self.assertEqual('e'*64,proof['input_package_sha256'])
        self.assertIn('enable: OFF',text)

    def test_derived_config_receipt_hash_and_replacement_tampering_are_rejected(self):
        mutations=(lambda c:c['configuration'].update(text=c['configuration']['text']+'# extra\n'),
                   lambda c:c['protocol_normalization'].update(reason='forged'),
                   lambda c:c['binding_identity'].update(protocol_normalization_sha256='0'*64),
                   lambda c:c['replacements'][-1].update(path='/other/user/config.yaml'),
                   lambda c:c['formal_manifest'][COMMON].update(sha256='0'*64))
        for mutate in mutations:
            template,candidate=self.old_schema();self.bind_proof(candidate);mutate(candidate)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):render(template,candidate,'bad-proof','actor')


if __name__ == "__main__":
    unittest.main()
