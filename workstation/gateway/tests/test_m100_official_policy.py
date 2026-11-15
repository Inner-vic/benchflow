import copy,hashlib,io,json,re,tarfile,unittest
from pathlib import Path
from unittest.mock import patch
from bench_gateway import m100_auto_inputs as inputs, candidate_adapter as adapter
from bench_gateway.m100_build import BuildError
import test_m100_auto_inputs as input_fixture
import test_m100_build as build_fixture


class OfficialInputsTests(unittest.TestCase):
    def setUp(self):
        self.h=input_fixture.AutoInputsTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.cmake=b'option(WITH_EVAL_DUMP "Enable offline eval dump" OFF)\n'
        self.archive()

    def archive(self):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as archive:
            item=tarfile.TarInfo('uroad-commit/CMakeLists.txt');item.size=len(self.cmake)
            archive.addfile(item,io.BytesIO(self.cmake))
        self.h.archive=stream.getvalue()

    def prepare(self):
        with patch.object(inputs,'load_authorization',return_value='fixture-auth'),patch.object(inputs,'source_token',return_value='fixture-token'):
            return inputs.prepare(self.h.cfg,self.h.store,'ou_test',self.h.pkg,fetch=self.h.fetch,follow_official=True)

    def definition(self,value):
        self.h.recipe+=('  self._cmake.definitions["WITH_EVAL_DUMP"] = '+value+'\n').encode()

    def test_cmake_off_is_preserved_without_goal_override(self):
        value=self.prepare();self.assertIs(False,value['eval_dump'])
        self.assertEqual('official-v1',value['eval_mode_policy'])
        self.assertEqual('locked-cmake-default',value['eval_mode_origin'])
        self.assertEqual(self.cmake,(Path(value['source'])/'uroad/CMakeLists.txt').read_bytes())
        self.assertIn('"WITH_EVAL_DUMP"] = False',(Path(value['source'])/'conanfile.py').read_text())
        self.h.urls.clear();self.assertEqual(value['source_snapshot_sha256'],self.prepare()['source_snapshot_sha256']);self.assertEqual([],self.h.urls)

    def test_explicit_official_on_takes_precedence_over_cmake_default(self):
        self.definition('True');value=self.prepare()
        self.assertIs(True,value['eval_dump']);self.assertEqual('locked-conan-definition',value['eval_mode_origin'])
        self.assertEqual(self.cmake,(Path(value['source'])/'uroad/CMakeLists.txt').read_bytes())

    def test_explicit_off_is_not_promoted_to_cmake_on(self):
        self.cmake=self.cmake.replace(b'OFF',b'ON');self.archive();self.definition('False')
        self.assertIs(False,self.prepare()['eval_dump'])

    def test_unknown_or_ambiguous_official_settings_stop(self):
        for recipe,cmake in [(self.h.recipe+b'  self._cmake.definitions["WITH_EVAL_DUMP"] = self.options.dump\n',self.cmake),
                             (self.h.recipe,b'cmake_minimum_required(VERSION 3.10)\n'),
                             (self.h.recipe,self.cmake+b'set(WITH_EVAL_DUMP ON)\n')]:
            with self.assertRaises(ValueError):inputs.official_eval_mode(recipe,cmake)

    def test_legacy_goal_selected_cache_is_not_used_or_overwritten(self):
        root=self.h.root/'build-inputs'/('a'*64);root.mkdir(parents=True)
        marker=root/'inputs-replay-only-v2.json';marker.write_text('{"legacy":true}')
        value=self.prepare();self.assertTrue(value['source'].endswith('source-official-v3'))
        self.assertEqual('{"legacy":true}',marker.read_text())

    def test_cached_mode_cannot_disagree_with_frozen_snapshot(self):
        value=self.prepare();marker=Path(value['source']).parent/'inputs-official-v3.json'
        cached=json.loads(marker.read_text());cached['eval_dump']=True;marker.write_text(json.dumps(cached))
        with self.assertRaises(ValueError):self.prepare()


class OfficialBuildTests(unittest.TestCase):
    def setUp(self):
        self.h=build_fixture.BuildTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.cfg['m100_follow_official_build_enabled']=True
        from bench_gateway.m100_profiles import resolve
        self.recipe=copy.deepcopy(resolve(self.h.cfg,self.h.package['data']['sha256']))
        self.recipe['build'].update(eval_mode_policy='official-v1',eval_dump=True)

    def test_only_replay_does_not_turn_off_official_on(self):
        self.h.cfg['replay_only_no_dump_enabled']=True;self.h.respond()
        with patch('bench_gateway.m100_profiles.resolve',return_value=self.recipe):
            self.h.call(goal='replay');first=self.h.request['key']
            self.h.call(goal='replay_and_evaluate')
        self.assertIs(True,self.h.request['identity']['build']['eval_dump'])
        self.assertEqual(first,self.h.request['key'])

    def test_official_off_for_evaluation_stops_before_compiler(self):
        self.recipe['build']['eval_dump']=False
        with patch('bench_gateway.m100_profiles.resolve',return_value=self.recipe):
            with self.assertRaises(BuildError) as error:self.h.call(goal='replay_and_evaluate')
        self.assertEqual('M100_OFFICIAL_OFF_FOR_EVALUATION',error.exception.code)
        self.h.ssh.python.assert_not_called()

    def test_unproven_old_recipe_is_not_used_as_official_variant(self):
        self.recipe['build'].pop('eval_mode_policy')
        with patch('bench_gateway.m100_profiles.resolve',return_value=self.recipe):
            with self.assertRaises(BuildError):self.h.call(goal='replay')
        self.h.ssh.python.assert_not_called()


class OfficialRuntimeTests(unittest.TestCase):
    def test_on_only_replay_retains_on_and_prediction_collection(self):
        fields={'eval_dump.enable':'ON'};before=copy.deepcopy(fields)
        self.assertFalse(adapter.official_replay_without_dump('replay',fields,True))
        self.assertEqual(before,fields)

    def test_off_only_replay_uses_completion_evidence(self):
        self.assertTrue(adapter.official_replay_without_dump('replay',{'eval_dump.enable':'OFF'},False))

    def test_off_cannot_satisfy_evaluation_or_enabled_runtime(self):
        for goal,fields,mode in [('replay_and_evaluate',{'eval_dump.enable':'OFF'},False),
                                 ('replay',{'eval_dump.enable':'ON'},False),
                                 ('replay_and_evaluate',{'eval_dump.enable':'OFF'},True),
                                 ('replay',{},None)]:
            with self.assertRaises(ValueError):adapter.official_replay_without_dump(goal,fields,mode)

    def test_uploaded_so_unknown_mode_does_not_claim_compilation_proof(self):
        self.assertFalse(adapter.official_replay_without_dump('replay',{'eval_dump.enable':'ON'},None))
        self.assertTrue(adapter.official_replay_without_dump('replay',{'eval_dump.enable':'OFF'},None))


class OfficialPreparationTests(unittest.TestCase):
    def test_goal_change_does_not_rebuild_verified_official_on_but_legacy_mode_does(self):
        from test_preparation_progress import PreparationProgressTests
        from bench_gateway.preparation import MaterialCheck
        for goal,policy,expected in [('replay','official-v1',0),('replay_and_evaluate','official-v1',0),('replay','legacy-goal-v2',1)]:
            h=PreparationProgressTests();h.setUp();self.addCleanup(h.doCleanups)
            h.cfg.update(m100_follow_official_build_enabled=True,replay_only_no_dump_enabled=True)
            h.running(goal=goal,m100_mode='provided',m100_so='https://example.test/liburoad_m100.so')
            service,fetch=h.material_service(verify_phases=False)
            def supplied(*args,**kwargs):
                asset=fetch(*args,**kwargs)
                if asset['id']=='asset-m100_so':
                    asset['data'].update(m100_automatic_build=True,eval_dump=True,eval_mode_policy=policy,
                        compiled_for_package_sha256='a'*64,compiled_sensor_sha256={'camera_so':'a'*64,'lidar_so':'a'*64})
                return asset
            with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.asset_fetch.fetch',side_effect=supplied),patch('bench_gateway.m100_build.build',return_value=(h.asset('m100_so'),{'state':'BUILT'})) as build:
                result=MaterialCheck(h.cfg,h.store).run(h.job)
            self.assertEqual('STATIC_PASSED',result['status']);self.assertEqual(expected,build.call_count)
            self.assertTrue(result['follow_official_build']);self.assertFalse(result['replay_only'])


class OfficialBindingTests(unittest.TestCase):
    def setup_binding(self,mode,goal):
        from test_candidate_remote import CandidateRemoteTests
        h=CandidateRemoteTests();h.setUp();self.addCleanup(h.doCleanups)
        h.use_reviewed_old_schema()
        with tarfile.open(h.package,'r:gz') as archive:
            entries=[(m.name,archive.extractfile(m).read()) for m in archive.getmembers() if m.isfile()]
        with tarfile.open(h.package,'w:gz') as archive:
            for name,data in entries:
                if name.endswith('/common_param.yaml'):
                    data,count=re.subn(rb'(eval_dump:\n\s+enable: )[^\n#]+',lambda m:m[1]+(b'ON' if mode else b'OFF'),data)
                    self.assertEqual(1,count)
                item=tarfile.TarInfo(name);item.size=len(data);archive.addfile(item,io.BytesIO(data))
        h.assets['package']['data']['sha256']=h.sha(h.package)
        # This is a synthetic APP: authorize its exact fixture hash for the
        # existing GT-field normalization without relaxing any production hash.
        from bench_gateway import candidate_adapter
        fixture_allow=patch.object(candidate_adapter,'NORMALIZATION_INPUTS',{(h.sha(h.package),h.assets['dependencies'][2]['data']['sha256'],h.template['id'])})
        fixture_allow.start();self.addCleanup(fixture_allow.stop)
        h.preparation['payload']={'goal':goal};h.preparation['result']['follow_official_build']=True
        h.assets['dependencies'][2]['data'].update(eval_mode_policy='official-v1',eval_dump=mode)
        h.adapter.config_fields=adapter.config_fields
        h.adapter.official_replay_without_dump=adapter.official_replay_without_dump
        h.adapter.configure_replay_only_batch=lambda module:None
        return h

    def test_off_binding_uses_completion_route_without_flipping_yaml(self):
        from bench_gateway import candidate_remote
        h=self.setup_binding(False,'replay');bound=h.bind();receipt=candidate_remote.read(bound['path'])
        self.assertTrue(receipt['candidate']['replay_only'])
        self.assertEqual('OFF',adapter.config_fields(receipt['candidate']['configuration']['text'])['eval_dump.enable'])
        h.m.execute.assert_not_called()

    def test_on_binding_only_replay_keeps_prediction_route(self):
        from bench_gateway import candidate_remote
        h=self.setup_binding(True,'replay');bound=h.bind();receipt=candidate_remote.read(bound['path'])
        self.assertFalse(receipt['candidate']['replay_only'])
        self.assertEqual('ON',adapter.config_fields(receipt['candidate']['configuration']['text'])['eval_dump.enable'])
        h.m.execute.assert_not_called()

    def test_off_binding_cannot_queue_evaluation(self):
        h=self.setup_binding(False,'replay_and_evaluate')
        with self.assertRaisesRegex(ValueError,'官方ON'):h.bind()
        h.m.execute.assert_not_called();h.m.command.assert_not_called()


if __name__=='__main__':unittest.main()
