import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bench_gateway import candidate_files
from bench_gateway import candidate_adapter
from bench_gateway import candidate_remote as remote
from bench_gateway import remote_agent


class CandidateRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "owner"
        self.root.mkdir()
        self.owner = "ou_candidate_remote"
        self.actor = "candidate-actor"
        self.package = self.root / "package.tar.gz"
        with tarfile.open(self.package, "w:gz") as archive:
            directory = tarfile.TarInfo("./")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            entries = [("livc/" + remote.COMMON, b"camera:\n  enabled: true\n")]
            entries += [("livc/lib/" + name, b"original-so") for name in ("libxcamera.so", "libxlidar.so", "liburoad_m100.so")]
            for name, raw in entries:
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                archive.addfile(item, io.BytesIO(raw))
        self.assets = {"package": self.asset(self.package, "package", "app"), "dependencies": []}
        for name in ("libxcamera.so", "libxlidar.so", "liburoad_m100.so"):
            path = self.root / name
            path.write_bytes(("replacement-" + name).encode())
            self.assets["dependencies"].append(self.asset(path, "dependency", name, target="lib/" + name))
        self.manifest = self.root / "manifest.json"
        prepared = [{"scene": "scene-one", "camera_frames": 1}, {"scene": "scene-two", "camera_frames": 1}]
        self.manifest.write_text(json.dumps({"prepared_scenes": prepared}), encoding="utf-8")
        cameras = []
        for index in (1, 2):
            directory = self.root / ("camera-%d" % index)
            directory.mkdir()
            (directory / "frame.jpeg").write_bytes(b"synthetic")
            cameras.append(str(directory))
        self.assets["dataset"] = {"id": "dataset-1", "owner": self.owner, "kind": "dataset", "name": "data", "version": 1,
                                  "data": {"remote_path": str(self.root), "manifest": str(self.manifest), "sha256": self.sha(self.manifest),
                                           "camera_roots": cameras, "scene_count": 2}}
        self.template = {"id": "reviewed-template", "historical_output_types": ["pred_road"]}
        self.controls = []
        self.control_hashes = {}
        for target in ("bin/bench-replay-mode", "bin/formal-replay-wrapper.sh", "sdf/infer_expert_pcm_replay.sdf"):
            path = self.base / Path(target).name
            path.write_bytes(("reviewed-" + target).encode())
            self.controls.append({"target": target, "path": str(path), "sha256": self.sha(path)})
            self.control_hashes[target] = self.sha(path)
        self.registered = {"source_run": self.template["id"], "template_id": "a" * 64, "controls": self.controls}
        self.implementations = {name: str(index) * 64 for index, name in enumerate(("candidate_files", "candidate_adapter", "candidate_remote", "batch"), 1)}
        resolved = {"package": self.assets["package"], "camera_so": self.assets["dependencies"][0],
                    "lidar_so": self.assets["dependencies"][1], "m100_so": self.assets["dependencies"][2]}
        self.preparation = {"id": "prep-remote", "owner": self.owner, "fingerprint": "b" * 64,
                            "result": {"status": "STATIC_PASSED", "assets": resolved, "dataset": self.assets["dataset"],
                                       "scenes": [1, 2], "yaml_preparation": {}}}
        self.request = {"owner": self.owner, "owner_root": str(self.root), "actor": self.actor, "operation": "bind_candidate",
                        "assets": self.assets, "preparation": self.preparation, "template": self.registered,
                        "candidate_implementation": self.implementations}
        self.m = SimpleNamespace(BASE=self.base / "runner", ROOT=self.base / "runner" / "automation",
                                 SAFE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"), SCENE=re.compile(r"^[A-Za-z0-9._-]+$"))
        for path in (self.m.BASE / "adapters", self.m.ROOT / "runs", self.m.ROOT / "tasks"):
            path.mkdir(parents=True, exist_ok=True)
        self.m.offline_validate = Mock(return_value={"runner_preflight": "READY", "profile": "VALID"})
        self.m.progress_event = Mock()
        self.m.command = Mock(return_value={"active": [], "queued": [], "waiting": []})
        self.m.bench_capacity = Mock(side_effect=AssertionError("no hardware expected"))
        def batch_execute(plan, before_submit=None):
            queue=self.m.command('queue')
            if queue.get('active') or queue.get('queued') or queue.get('waiting'):return {'state':'RESOURCE_BUSY'}
            if before_submit is not None:before_submit()
            return {"state": "PASS", "run_id": plan["run_id"], "task": str(self.m.ROOT / "tasks" / plan["run_id"])}
        self.m.execute = Mock(side_effect=batch_execute)
        self.m.validate_and_retain = Mock(return_value=(Path("retained"), Path("receipt")))
        self.m.finish_delivery = Mock(side_effect=lambda plan, code: {"state": "PASS", "run_id": plan["run_id"], "task": "retained"})
        self.m.write = lambda path, value: Path(path).write_text(json.dumps(value), encoding="utf-8")
        self.adapter = SimpleNamespace(render=self.render,configuration_overrides=candidate_adapter.configuration_overrides,
            protocol_normalize_common=candidate_adapter.protocol_normalize_common,
            validate_protocol_normalization=candidate_adapter.validate_protocol_normalization,
            normalization_digest=candidate_adapter.normalization_digest)
        self.template_patch = patch.object(remote, "load_template", return_value=self.template)
        self.template_patch.start()
        self.addCleanup(self.template_patch.stop)
        self.control_patch = patch.object(remote, "CONTROL_HASHES", self.control_hashes, create=True)
        self.control_patch.start()
        self.addCleanup(self.control_patch.stop)

    @staticmethod
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def asset(self, path, kind, name, **data):
        return {"id": "asset-" + name, "owner": self.owner, "kind": kind, "name": name, "version": 1,
                "data": {"remote_path": str(path), "sha256": self.sha(path), **data}}

    @staticmethod
    def render(template, candidate, run_id, actor, indices):
        return {"job": {"run_id": run_id, "actor": actor, "indices": indices, "package": candidate["package"]},
                "profile": {"profile_id": run_id, "argv": ["administrator-reviewed-command", run_id]},
                "adapter": "# administrator adapter for " + run_id,
                "driver": "# administrator driver for " + run_id,
                "identity": {"required_output_types": ["pred_img", "pred_traj"]}}

    def dataset(self, req, root, asset):
        if asset != self.assets["dataset"] or self.sha(self.manifest) != asset["data"]["sha256"]:
            raise ValueError("dataset mismatch")
        return self.manifest, [self.root]

    def bind(self, req=None):
        return remote.bind(req or self.request, self.m, candidate_files, self.adapter, self.dataset)

    def use_reviewed_old_schema(self,assembly=False):
        from test_candidate_adapter import fixture
        _,example=fixture()
        raw=example['original_configuration_text'].replace('  gt_trajectory:\n    enable: ON\n','').encode()
        with tarfile.open(self.package,'w:gz') as archive:
            entries=[('livc/'+remote.COMMON,raw)]+[('livc/lib/'+name,b'original-so')
                for name in ('libxcamera.so','libxlidar.so','liburoad_m100.so')]
            for name,data in entries:
                item=tarfile.TarInfo(name);item.size=len(data);archive.addfile(item,io.BytesIO(data))
        self.assets['package']['data']['sha256']=self.sha(self.package)
        source=self.sha(self.package)
        if assembly:
            source='f'*64
            self.assets['package']['origin']='assembly_catalog'
            self.assets['package']['data']['package_source_sha256']=source
        m100=self.assets['dependencies'][2]['data']['sha256']
        allowed=patch.object(candidate_adapter,'NORMALIZATION_INPUTS',{(source,m100,self.template['id'])})
        allowed.start();self.addCleanup(allowed.stop)
        return raw

    def test_old_schema_binding_installs_real_derived_common_and_preserves_original_inputs(self):
        original=self.use_reviewed_old_schema();package_before=self.package.read_bytes()
        request=self.bound_request();receipt=remote.read(request['baseline']['binding']['path'])
        candidate=receipt['candidate'];proof=candidate['protocol_normalization']
        self.assertEqual(original.decode(),candidate['original_configuration_text'])
        self.assertEqual(package_before,self.package.read_bytes())
        self.assertNotIn('protocol_normalization',receipt['frozen'])
        self.assertEqual({},candidate['yaml_preparation'])
        self.assertEqual(proof,receipt['execution_policy']['protocol_normalization'])
        derived=candidate['replacements'][-1]
        self.assertEqual('protocol_normalization',derived['kind'])
        self.assertEqual(remote.COMMON,derived['target'])
        self.assertEqual(proof['after_sha256'],self.sha(derived['path']))
        self.assertEqual(proof['after_sha256'],candidate['formal_manifest'][remote.COMMON]['sha256'])
        self.assertEqual(receipt['binding_identity'],candidate['binding_identity'])
        self.m.bench_capacity.side_effect=None
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as install:
            self.assertEqual('PASS',self.execute(request,matches=Mock(side_effect=[False,True]))['state'])
        argv=install.call_args.args[0]
        self.assertIn(remote.COMMON+'='+derived['path'],argv)
        self.assertEqual('--replace',argv[argv.index(remote.COMMON+'='+derived['path'])-1])

    def test_embedded_m100_still_supplies_the_required_installer_argument(self):
        request=self.bound_request();receipt=remote.read(request['baseline']['binding']['path'])
        candidate=receipt['candidate'];candidate['embedded_dependencies']={x['target']:x['sha256'] for x in candidate['replacements']}
        self.m.bench_capacity.side_effect=None
        with patch.object(remote,'load_binding',return_value=(receipt,self.template)),patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as install:
            self.assertEqual('PASS',self.execute(request,matches=Mock(side_effect=[False,True]))['state'])
        argv=install.call_args.args[0];m100=next(x for x in candidate['replacements'] if x['target']=='lib/liburoad_m100.so')
        self.assertEqual(m100['path'],argv[argv.index('--m100-so')+1])
        self.assertFalse(any('libxcamera.so=' in x or 'libxlidar.so=' in x for x in argv))

    def test_normalization_load_rederives_proof_and_rejects_inner_tampering(self):
        self.use_reviewed_old_schema();request=self.bound_request();path=Path(request['baseline']['binding']['path'])
        original=remote.read(path)
        mutations=(lambda r:r['candidate'].update(original_configuration_text=r['candidate']['original_configuration_text']+'# changed\n'),
                   lambda r:r['candidate']['protocol_normalization'].update(reason='unreviewed'),
                   lambda r:r['candidate'].update(package_origin='assembly_catalog',package_source_sha256='f'*64),
                   lambda r:r['candidate']['replacements'][-1].update(kind='yaml'),
                   lambda r:r['candidate']['formal_manifest'][remote.COMMON].update(sha256='0'*64),
                   lambda r:r['execution_policy'].pop('protocol_normalization'))
        for index,mutate in enumerate(mutations):
            receipt=copy.deepcopy(original)
            mutate(receipt)
            path.chmod(0o600);path.write_bytes(remote.canonical(receipt));request['baseline']['binding']['sha256']=self.sha(path)
            with self.subTest(index=index),self.assertRaises(ValueError):
                remote.load_binding(request,self.m,candidate_files,self.dataset,adapter=self.adapter)
        self.m.execute.assert_not_called();self.m.command.assert_not_called()

    def test_normalization_accepts_only_assembly_lineage_frozen_in_owner_asset(self):
        self.use_reviewed_old_schema(assembly=True);request=self.bound_request()
        receipt,_=remote.load_binding(request,self.m,candidate_files,self.dataset,adapter=self.adapter)
        self.assertEqual('f'*64,receipt['candidate']['protocol_normalization']['package_sha256'])
        self.assertEqual(self.sha(self.package),receipt['candidate']['protocol_normalization']['input_package_sha256'])
        changed=copy.deepcopy(request);changed['assets']['package'].pop('origin')
        with self.assertRaises(ValueError):remote.load_binding(changed,self.m,candidate_files,self.dataset,adapter=self.adapter)

    def bound_request(self, operation="smoke", **extra):
        reference = self.bind()
        request = {"owner": self.owner, "owner_root": str(self.root), "assets": copy.deepcopy(self.assets),
                   "actor": self.actor, "operation": operation, "run_id": "job-now-smoke", "scenes": [1],
                   "candidate_implementation": copy.deepcopy(self.implementations),
                   "baseline": {"kind": "candidate-v1", "binding": reference, "preparation_id": self.preparation["id"],
                                "preparation_fingerprint": self.preparation["fingerprint"]}}
        request.update(extra)
        return request

    def execute(self, request, install=None, matches=None):
        return remote.execute(request, self.m, candidate_files, self.adapter, self.dataset,
                              install or Mock(return_value=Path("administrator-installer.py")),
                              matches or Mock(return_value=True))

    def materialize(self, request, run_id, scenes):
        receipt, template = remote.load_binding(request, self.m, candidate_files, self.dataset, adapter=self.adapter)
        plan = remote.make_plan(self.m, self.render, template, receipt["candidate"], run_id, self.actor, scenes)
        remote.configure_batch(self.m)
        self.m.prepare(plan)
        return plan

    def test_bind_only_runs_offline_validation_and_preserves_materials(self):
        before = self.package.read_bytes()
        with patch.object(remote_agent, "formal_matches", side_effect=AssertionError("hardware probe forbidden")), \
             patch.object(remote_agent, "runtime_installer", side_effect=AssertionError("install forbidden")), \
             patch("subprocess.run", side_effect=AssertionError("subprocess forbidden")):
            reference = self.bind()
        self.assertEqual("READY_FOR_GATE", reference["state"])
        self.assertEqual("NOT_RUN", reference["hardware_actions"])
        self.assertEqual(2, self.m.offline_validate.call_count)
        self.m.command.assert_not_called()
        self.m.execute.assert_not_called()
        self.m.bench_capacity.assert_not_called()
        self.assertEqual(before, self.package.read_bytes())
        receipt = remote.read(reference["path"])
        self.assertFalse(receipt["gate_verified"])
        self.assertEqual([1, 2], receipt["binding_identity"]["scenes"])
        for call in self.m.offline_validate.call_args_list:
            self.assertEqual(["pred_img", "pred_traj"], call.args[0]["required_output_types"])

    def test_bind_refuses_materials_different_from_static_preparation(self):
        request = copy.deepcopy(self.request)
        request["assets"] = copy.deepcopy(request["assets"])
        request["assets"]["package"]["version"] += 1
        with self.assertRaises(ValueError):
            self.bind(request)

    def test_load_rejects_cross_owner_reference_before_template_or_files(self):
        request = self.bound_request()
        foreign = self.base / "foreign.json"
        foreign.write_bytes(Path(request["baseline"]["binding"]["path"]).read_bytes())
        request["baseline"]["binding"]["path"] = str(foreign)
        with self.assertRaisesRegex(ValueError, "outside owner"):
            remote.load_binding(request, self.m, candidate_files, self.dataset)

    def test_load_rejects_modified_receipt_configuration_inputs_and_implementation(self):
        request = self.bound_request()
        path = Path(request["baseline"]["binding"]["path"])
        original = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            remote.load_binding(request, self.m, candidate_files, self.dataset)
        path.write_bytes(original)
        receipt = remote.read(path)
        config = Path(receipt["candidate"]["configuration"]["remote_path"])
        config.chmod(0o600)
        data = config.read_bytes()
        config.write_bytes(data + b"# changed\n")
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            remote.load_binding(request, self.m, candidate_files, self.dataset)
        config.write_bytes(data)
        altered = copy.deepcopy(request)
        altered["assets"]["package"]["version"] += 1
        with self.assertRaisesRegex(ValueError, "queued inputs differ"):
            remote.load_binding(altered, self.m, candidate_files, self.dataset)
        altered = copy.deepcopy(request)
        altered["candidate_implementation"]["candidate_adapter"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "implementation changed"):
            remote.load_binding(altered, self.m, candidate_files, self.dataset)

    def test_load_rejects_scene_outside_binding(self):
        request = self.bound_request(scenes=[3])
        with self.assertRaisesRegex(ValueError, "outside bound"):
            remote.load_binding(request, self.m, candidate_files, self.dataset)

    def test_smoke_requires_first_selected_scene_and_returns_frozen_identity(self):
        request = self.bound_request()
        result = self.execute(request)
        self.assertEqual(request["baseline"]["binding"]["input_digest"], result["input_digest"])
        self.assertEqual("job-now-smoke", result["run_id"])
        self.assertEqual(["pred_img", "pred_traj"], self.m.execute.call_args.args[0]["required_output_types"])
        request["scenes"] = [2]
        with self.assertRaisesRegex(ValueError, "first selected"):
            self.execute(request)

    def test_replay_rejects_missing_or_other_task_smoke_before_dispatch(self):
        request = self.bound_request(operation="replay", run_id="job-now", scenes=[1, 2])
        for smoke in (None, "old-job-smoke"):
            request.update(smoke_run=smoke, smoke_scenes=[1])
            with self.assertRaisesRegex(ValueError, "this task"):
                self.execute(request)
        self.m.command.assert_not_called()
        self.m.execute.assert_not_called()

    def test_replay_verifies_same_task_smoke_code_and_runner_receipts(self):
        request = self.bound_request(operation="replay", run_id="job-now", scenes=[1, 2], smoke_run="job-now-smoke", smoke_scenes=[1])
        self.materialize(request, "job-now-smoke", [1])
        result = self.execute(request)
        self.assertEqual("PASS", result["state"])
        self.assertEqual("job-now-smoke", self.m.validate_and_retain.call_args.args[0]["run_id"])
        self.m.execute.reset_mock()
        adapter = self.m.BASE / "adapters/job-now-smoke/small_stream_adapter.py"
        adapter.write_text("# modified by caller", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "adapter code differs"):
            self.execute(request)
        self.m.execute.assert_not_called()

    def test_failed_smoke_receipt_validation_stops_before_queue_install_and_execute(self):
        request = self.bound_request(operation="replay", run_id="job-now", scenes=[1, 2], smoke_run="job-now-smoke", smoke_scenes=[1])
        self.materialize(request, "job-now-smoke", [1])
        self.m.validate_and_retain.side_effect = RuntimeError("restoration receipt failed")
        install, matches = Mock(), Mock()
        with self.assertRaisesRegex(RuntimeError, "restoration"):
            self.execute(request, install, matches)
        install.assert_not_called()
        matches.assert_not_called()
        self.m.command.assert_not_called()
        self.m.execute.assert_not_called()

    def test_busy_resource_does_not_install_or_execute(self):
        request = self.bound_request()
        self.m.command.return_value = {"active": [{"run_id": "colleague"}]}
        install, matches = Mock(), Mock()
        self.assertEqual("RESOURCE_BUSY", self.execute(request, install, matches)["state"])
        install.assert_not_called()
        matches.assert_not_called()
        self.m.execute.assert_called_once()
        self.m.command.assert_called_once_with('queue')

    def test_diagnosis_survives_removed_inputs_and_adapter_upgrade_without_requalification(self):
        request=self.bound_request(operation='diagnose')
        self.package.unlink()
        request['candidate_implementation']['candidate_adapter']='e'*64
        run=self.m.ROOT/'runs'/request['run_id'];run.mkdir()
        (run/'restore-verification.json').write_text('{"restore_ok":true}',encoding='utf-8')
        (run/'probe.log').write_text('candidate startup RESTART_LOOP; inspect startup evidence',encoding='utf-8')
        with patch.object(remote,'load_template',side_effect=AssertionError('no requalification')), \
             patch.object(candidate_files,'freeze_inputs',side_effect=AssertionError('no rehash')):
            result=self.execute(request)
        self.assertEqual('DIAGNOSED',result['state'])
        self.assertEqual('APPLICATION_CRASH',result['category'])
        self.assertTrue(result['safe_to_continue'])
        self.m.execute.assert_not_called();self.m.command.assert_not_called()

    def test_diagnosis_still_rejects_changed_owner_binding(self):
        request=self.bound_request(operation='diagnose');request['owner']='someone-else'
        with self.assertRaisesRegex(ValueError,'identity mismatch'):self.execute(request)

    def test_diagnosis_does_not_release_barrier_for_dangling_lock(self):
        request=self.bound_request(operation='diagnose')
        run=self.m.ROOT/'runs'/request['run_id'];run.mkdir()
        (run/'restore-verification.json').write_text('{"restore_ok":true}',encoding='utf-8')
        lock=self.m.BASE/'locks/bench.lock';lock.parent.mkdir(exist_ok=True)
        lock.symlink_to(lock.parent/'missing-owner')
        result=self.execute(request)
        self.assertFalse(result['safe_to_continue'])
        self.assertTrue(lock.is_symlink())

    def test_delivery_recovery_reuses_exact_code_without_replay_or_install(self):
        request = self.bound_request(operation="recover_delivery", run_id="job-now", scenes=[1, 2])
        self.materialize(request, "job-now", [1, 2])
        install, matches = Mock(), Mock()
        result = self.execute(request, install, matches)
        self.assertEqual("PASS", result["state"])
        self.assertEqual(request["baseline"]["binding"]["input_digest"], result["input_digest"])
        self.m.finish_delivery.assert_called_once()
        self.m.command.assert_not_called()
        self.m.execute.assert_not_called()
        install.assert_not_called()
        matches.assert_not_called()

    def test_resume_rejects_changed_job_profile_binding_and_driver(self):
        request = self.bound_request(operation="recover_delivery", run_id="job-now", scenes=[1, 2])
        self.materialize(request, "job-now", [1, 2])
        directory = self.m.BASE / "adapters/job-now"
        for name in ("job.json", "profile.json", "gateway-binding.json", "sw0058_gate_probe_driver_reference.py"):
            with self.subTest(name=name):
                path = directory / name
                original = path.read_bytes()
                path.write_text("{}" if name.endswith("json") else "# tampered", encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.execute(request)
                path.write_bytes(original)

    def test_make_plan_checks_scene_entry_without_enumerating_camera_frames(self):
        request = self.bound_request()
        receipt, template = remote.load_binding(request, self.m, candidate_files, self.dataset)
        (self.m.ROOT / "runs/reused").mkdir()
        with self.assertRaisesRegex(ValueError, "already used"):
            remote.make_plan(self.m, self.render, template, receipt["candidate"], "reused", self.actor, [1])
        with patch.object(Path,'iterdir',side_effect=AssertionError('no routine frame enumeration')):
            remote.make_plan(self.m, self.render, template, receipt["candidate"], "new-run", self.actor, [1])
        camera=Path(self.assets["dataset"]["data"]["camera_roots"][0])
        camera.rename(camera.with_name(camera.name+'-moved'))
        with self.assertRaisesRegex(ValueError, "camera source changed"):
            remote.make_plan(self.m, self.render, template, receipt["candidate"], "new-run", self.actor, [1])

    def test_template_requalification_change_is_rejected(self):
        self.template_patch.stop()
        with patch.object(remote, "qualify", return_value={**self.registered, "template_id": "changed"}):
            with self.assertRaisesRegex(ValueError, "template changed"):
                remote.load_template(self.m, self.registered)

    def test_remote_candidate_hash_is_checked_before_batch_exec(self):
        # No injected source is executed in this regression test: batch_module
        # is replaced by a spy, so a missing pre-exec check becomes a failure.
        source = b"# deliberately different candidate batch"
        request = {"owner_root": str(self.root), "operation": "bind_candidate", "batch_code": base64.b64encode(source).decode(),
                   "candidate_implementation": {**self.implementations, "batch": "0" * 64}}
        batch = Mock(return_value=self.m)
        module = SimpleNamespace(bind=Mock(return_value={"state": "UNEXPECTED"}))
        with patch.object(remote_agent, "batch_module", batch), \
             patch.object(remote_agent, "candidate_modules", return_value={"candidate_remote": module, "candidate_files": candidate_files, "candidate_adapter": self.adapter}):
            with self.assertRaises(ValueError):
                remote_agent.main(request)
        batch.assert_not_called()

    def test_remote_rejects_traversal_run_id_before_status_or_diagnosis(self):
        for operation in ("status", "diagnose"):
            with self.subTest(operation=operation):
                request = {"owner_root": str(self.root), "operation": operation, "run_id": "../foreign", "batch_code": ""}
                with patch.object(remote_agent, "batch_module", return_value=self.m), self.assertRaises(ValueError):
                    remote_agent.main(request)

    def test_remote_rejects_changed_yaml_runtime_and_candidate_module_bytes(self):
        expected = "a" * 64
        request = {"candidate_implementation": self.implementations,
                   "yaml_runtime": {"path": "/home/lixiang/bench-gateway/yaml-runtime/" + expected + "/pyyaml.zip", "sha256": expected},
                   "candidate_code": {name: base64.b64encode(b'raise AssertionError("must not execute")').decode()
                                      for name in ("candidate_files", "candidate_adapter", "candidate_remote")}}
        # No real runtime filesystem is read: a wrong digest must fail before
        # interpreting any encoded source.
        with patch.object(Path, "resolve", lambda path, *args, **kwargs: path), patch.object(remote_agent, "sha", return_value="b" * 64):
            with self.assertRaisesRegex(ValueError, "YAML runtime changed"):
                remote_agent.candidate_modules(request)
        before_path, before_flag = list(sys.path), sys.dont_write_bytecode
        try:
            with patch.object(Path, "resolve", lambda path, *args, **kwargs: path), patch.object(remote_agent, "sha", return_value=expected):
                with self.assertRaisesRegex(ValueError, "implementation changed"):
                    remote_agent.candidate_modules(request)
        finally:
            sys.path[:] = before_path
            sys.dont_write_bytecode = before_flag


if __name__ == "__main__":
    unittest.main()
