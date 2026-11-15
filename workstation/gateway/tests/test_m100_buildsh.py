import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from bench_gateway import m100_ci as ci, m100_auto_inputs as inputs, m100_build_remote as remote
import test_m100_auto_inputs as fixture
import test_m100_build as build_fixture


def response(number=2880):
    return {'number': number, 'building': False, 'result': 'SUCCESS', 'actions': [
        {'parameters': [{'name': 'APP_VERSION', 'value': 'conan_shared/1.0.26-avm@livc/release-schu\ninfer_expert_sdk/0.1.89-rc2@livc/stable'},
                        {'name': 'LOCKED_LIBS', 'value': 'function-message/v0.0.335@ad/release'},
                        {'name': 'PORTAL_RESOURCE_KEY', 'value': 'never-save-this'}]}]}


class CiTests(unittest.TestCase):
    def test_configuration_is_encrypted_only_after_successful_login(self):
        with patch.object(ci,'_save_basic') as save:
            with self.assertRaises(ValueError):
                ci.configure({'project_root':'/private'},'fixture','fixture-password',fetch=Mock(return_value=b'{"authenticated":false}'))
            save.assert_not_called()
            public=ci.configure({'project_root':'/private'},'fixture','fixture-password',fetch=Mock(return_value=b'{"authenticated":true}'))
            save.assert_called_once();self.assertNotIn('fixture-password',json.dumps(public))

    def test_parameters_accept_sdk_in_either_ci_list_and_exclude_unrelated_secrets(self):
        result = ci.parameters(response(), 2880)
        self.assertEqual('conan_shared/1.0.26-avm@livc/release-schu', result['baselock'])
        self.assertEqual('function-message/v0.0.335@ad/release', result['build_requirement_pins']['function-message'])
        self.assertNotIn('never-save-this', json.dumps(result))

    def test_wrong_or_unfinished_build_missing_conflicting_and_ranged_refs_fail(self):
        for change in [dict(number=9), dict(building=True), dict(result='FAILURE')]:
            d=response();d.update(change)
            with self.assertRaises(ValueError):ci.parameters(d,2880)
        for value in ['', 'conan_shared/[>0.0]@livc/release-schu',
                      'conan_shared/1@livc/release-schu\nconan_shared/2@livc/release-schu',
                      'conan_shared/1@livc/release-x64',
                      'conan_shared/1@livc/release-schu; echo unsafe']:
            d=response();d['actions'][0]['parameters'][0]['value']=value
            with self.assertRaises(ValueError):ci.parameters(d,2880)
        d=response();d['actions'][0]['parameters'][1]['value']+='\ninfer_expert_sdk/other@livc/stable'
        with self.assertRaises(ValueError):ci.parameters(d,2880)

    def test_reference_is_fixed_job_and_auth_is_separate(self):
        for url in ['https://u:p@'+ci.HOST+ci.JOB+'2880/', 'https://'+ci.HOST+':8443'+ci.JOB+'2880/',
                    'https://'+ci.HOST+'/job/other/2880/', 'https://'+ci.HOST+ci.JOB+'2880/?x=y']:
            with self.assertRaises(ValueError):ci.build_url(url,2880)
        fetch=Mock(return_value=json.dumps(response()).encode())
        with patch.object(ci,'authorization',return_value='Basic fixture'):
            result=ci.read({},'',2880,fetch)
        self.assertEqual('Basic fixture',fetch.call_args.args[1]['Authorization'])
        self.assertEqual(ci.build_url('',2880),result['build_reference'])

    def test_missing_ci_auth_never_fetches_and_missing_build_never_falls_back(self):
        fetch=Mock()
        with patch.object(ci,'authorization',return_value=None):
            with self.assertRaises(inputs.InputError) as e:ci.read({},'',2880,fetch)
        self.assertEqual('M100_CI_AUTH_REQUIRED',e.exception.code);fetch.assert_not_called()
        from urllib.error import HTTPError
        fetch.side_effect=HTTPError('unused',404,'missing',None,None)
        with patch.object(ci,'authorization',return_value='Basic fixture'):
            with self.assertRaises(inputs.InputError) as e:ci.read({},'',2880,fetch)
        self.assertEqual('M100_CI_PARAMETERS_UNAVAILABLE',e.exception.code)
        self.assertEqual(1,fetch.call_count)


class BuildshInputsTests(unittest.TestCase):
    def setUp(self):
        self.h=fixture.AutoInputsTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.script=b'#!/bin/bash\nset -e\n# reviewed original fixture\n'
        self.cmake=b'option(WITH_EVAL_DUMP "Enable offline eval dump" OFF)\n'
        # Simulate the repository recipe; the exported recipe is independently
        # used for the immutable commit/official switch evidence.
        self.local=self.h.recipe.replace(b'old@',b'local-old@')
        self.ci_response=response()
        self.h.cfg.update(m100_buildsh_enabled=True,m100_reviewed_buildsh_sha256=[inputs.sha(self.script)])

    def prepare(self,mode=None):
        if mode is not None:self.h.recipe+=('  self._cmake.definitions["WITH_EVAL_DUMP"] = '+str(mode)+'\n').encode()
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as archive:
            for name,body in [('CMakeLists.txt',self.cmake),('conanfile.py',self.local),('build.sh',self.script)]:
                item=tarfile.TarInfo('root/'+name);item.size=len(body);item.mode=0o755 if name=='build.sh' else 0o644
                archive.addfile(item,io.BytesIO(body))
        self.h.archive=stream.getvalue()
        def fetch(url,*args):
            if url.startswith('https://'+ci.HOST):return json.dumps(self.ci_response).encode()
            return self.h.fetch(url,*args)
        with patch.object(inputs,'load_authorization',return_value='fixture-jfrog'),patch.object(inputs,'source_token',return_value='fixture-gitlab'),patch.object(ci,'authorization',return_value='fixture-ci'):
            return inputs.prepare(self.h.cfg,self.h.store,'ou_test',self.h.pkg,fetch=fetch,follow_official=True)

    def test_uses_original_root_script_and_ci_pins_keeps_official_mode(self):
        for mode in (True,False):
            if mode is False:
                self.h.pkg['data']['sha256']='f'*64
                self.h.recipe=self.h.recipe.replace(b'"WITH_EVAL_DUMP"] = True',b'"WITH_EVAL_DUMP"] = False')
                result=self.prepare()
            else:result=self.prepare(mode)
            source=Path(result['source'])
            self.assertEqual(self.script,(source/'build.sh').read_bytes())
            self.assertEqual(self.cmake,(source/'CMakeLists.txt').read_bytes())
            self.assertIs(mode,result['eval_dump'])
            self.assertIn('local-old',self.local.decode())
            self.assertNotIn('local-old',(source/'conanfile.py').read_text())
            self.assertEqual('upstream-buildsh-v1',result['build_entry'])
            self.assertNotIn('never-save-this',json.dumps(result))
            from build_uroad_eval import validate_source_snapshot
            self.assertEqual(result['ci'],validate_source_snapshot(source,source/'source-snapshot.json')['ci'])

    def test_unreviewed_script_and_inconsistent_ci_lock_fail_without_compiling(self):
        self.h.cfg['m100_reviewed_buildsh_sha256']=[]
        with self.assertRaises(inputs.InputError) as e:self.prepare(False)
        self.assertEqual('M100_BUILD_SCRIPT_REVIEW_REQUIRED',e.exception.code)
        pins=ci.parameters(response(),2880);pins['build_requirement_pins']['infer_expert_sdk']='infer_expert_sdk/other@livc/stable'
        with self.assertRaises(ValueError):ci.verify_pins(pins,self.h.document)

    def test_v3_cache_cannot_substitute_for_original_buildsh(self):
        root=self.h.root/'build-inputs'/('a'*64);root.mkdir(parents=True)
        old=root/'inputs-official-v3.json';old.write_text('{"old":true}')
        result=self.prepare(False)
        self.assertTrue(result['source'].endswith('source-buildsh-v5'))
        self.assertEqual('{"old":true}',old.read_text())
        marker=root/'inputs-buildsh-v5.json';d=json.loads(marker.read_text());d['ci']['baselock']='conan_shared/other@livc/release-schu';marker.write_text(json.dumps(d))
        with self.assertRaises(ValueError):self.prepare()

    def test_safe_askpass_recipe_with_concatenated_immutable_commit_is_supported(self):
        recipe=('class R:\n name="uroad_m100"\n branch="'+'b'*40+'"\n'
                ' def source(self):\n  self.run("git fetch https://gitlab.chehejia.com/livc-acore/chassis/uroad.git " + self.branch)\n').encode()
        self.assertEqual('b'*40,inputs.recipe_source(recipe))

    def test_official_cmake_string_on_off_are_not_python_truthiness(self):
        for text,expected in [('ON',True),('OFF',False),('TRUE',True),('FALSE',False)]:
            recipe=('class R:\n def m(self):\n  self._cmake.definitions["WITH_EVAL_DUMP"] = '+repr(text)+'\n').encode()
            self.assertEqual((expected,'locked-conan-definition'),inputs.official_eval_mode(recipe,self.cmake))

    def test_official_ci_lidar_override_is_applied_instead_of_local_old_reference(self):
        pin='xlidar/0.2.8-pcm@livc/pcm'
        self.ci_response['actions'][0]['parameters'][1]['value']+='\n'+pin
        self.h.document['graph_lock']['nodes']['3']={'ref':pin+'#'+'f'*32}
        self.h.document['graph_lock']['nodes']['0']['requires'].append('3')
        self.local+=b' def sensor(self):\n  self.requires(f"xlidar/0.2.8@livc/stable")\n'
        value=self.prepare(True)
        self.assertIn(pin,(Path(value['source'])/'conanfile.py').read_text())
        self.assertEqual({'xlidar':pin},value['ci']['sensor_development_pins'])


class OriginalInvocationTests(unittest.TestCase):
    def test_cmake_cache_true_false_are_equivalent_to_on_off_but_not_each_other(self):
        from build_uroad_eval import verify_effective_mode,BuildError
        for value,expected in [('True',True),('ON',True),('False',False),('OFF',False)]:
            verify_effective_mode('WITH_EVAL_DUMP:BOOL='+value+'\n',expected)
            with self.assertRaises(BuildError):verify_effective_mode('WITH_EVAL_DUMP:BOOL='+value+'\n',not expected)
        for cache in ['','WITH_EVAL_DUMP:BOOL=UNKNOWN\n','WITH_EVAL_DUMP:BOOL=ON\nWITH_EVAL_DUMP:BOOL=OFF\n']:
            with self.assertRaises(BuildError):verify_effective_mode(cache,True)

    def test_cold_build_budget_expands_only_for_original_script(self):
        from bench_gateway.m100_profiles import resolve
        for original,expected in [(True,600),(False,300)]:
            h=build_fixture.BuildTests();h.setUp();self.addCleanup(h.doCleanups)
            recipe=copy.deepcopy(resolve(h.cfg,h.package['data']['sha256']))
            if original:
                h.cfg.update(m100_buildsh_enabled=True,m100_follow_official_build_enabled=True)
                recipe['build'].update(build_entry='upstream-buildsh-v1',eval_mode_policy='official-v1',eval_dump=True)
            h.respond()
            with patch('bench_gateway.m100_profiles.resolve',return_value=recipe):h.call(seconds=800,goal='replay')
            self.assertEqual(expected,h.request['build_seconds'])

    def test_private_seed_skips_interrupted_packages_without_changing_shared_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);attempt=root/'attempt';attempt.mkdir();cache=root/'cache';source=root/'source';(source/'build').mkdir(parents=True)
            entry=cache/'sdk/1/livc/stable';(entry/'package'/('a'*40)).mkdir(parents=True)
            (entry/'package'/('b'*40)).mkdir();(entry/'package'/('b'*40+'.dirty')).write_text('')
            (entry/'export').mkdir();(entry/'export/conanfile.py').write_text('# fixture')
            (entry/'metadata.json').write_text('{}');(entry/'build/unused').mkdir(parents=True)
            (source/'build/conan.lock').write_text(json.dumps({'graph_lock':{'nodes':{'0':{'ref':'sdk/1@livc/stable#'+'c'*32}}}}))
            build={'cache':str(cache),'source':str(source),'baselock':'conan_shared/1@livc/release-schu'}
            request={'credential_root':str(root),'credential_user':'fixture'}
            secret={'username':'fixture','password':'fixture-secret','gitlab_token':'fixture-token'}
            with patch.object(remote.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(secret))):
                remote.private_build_environment(attempt,build,request)
            packages=attempt/'conan-home/data/sdk/1/livc/stable/package'
            self.assertTrue((packages/('a'*40)).is_symlink());self.assertFalse((packages/('b'*40)).exists())
            self.assertFalse((packages.parent/'build').exists());self.assertTrue((entry/'build/unused').is_dir())
            self.assertEqual(0o600,(attempt/'transient-auth/credentials.json').stat().st_mode&0o777)

    def test_exact_documented_argv_and_generated_mode_lock_receipt(self):
        from build_uroad_eval import main
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source';source.mkdir();(source/'build').mkdir()
            (source/'conanfile.py').write_text('class R:\n def m(self):\n  self._cmake.definitions["WITH_EVAL_DUMP"] = False\n')
            (source/'build.sh').write_text('# original fixture')
            ci_proof=ci.parameters(response(),2880)
            proof={'source_commit':'b'*40,'recipe_patch_sha256':'d'*64,'build_entry':'upstream-buildsh-v1',
                   'ci':ci_proof,'build_script_sha256':hashlib.sha256((source/'build.sh').read_bytes()).hexdigest()}
            def invoke(argv,**kwargs):
                self.assertEqual(['./build.sh','-t','schu','-o','platform=schu','-s','-v',ci_proof['baselock']],argv[-8:])
                out=source/'install/lib';out.mkdir(parents=True)
                elf=bytearray(64);elf[:6]=b'\x7fELF\x02\x01';elf[18:20]=(183).to_bytes(2,'little');(out/'liburoad_m100.so').write_bytes(elf)
                (source/'build/CMakeCache.txt').write_text('WITH_EVAL_DUMP:BOOL=OFF\n')
                lock={'graph_lock':{'nodes':{str(i):{'ref':ref+'#'+'a'*32} for i,ref in enumerate(ci_proof['build_requirement_pins'].values())}}}
                (source/'build/conan_shared.lock').write_text(json.dumps(lock));return Mock(returncode=0)
            argv=['build','--source',str(source),'--expected-commit','b'*40,'--artifact-id','artifact',
                  '--artifacts-root',str(Path(temp)/'out'),'--baselock',ci_proof['baselock'],
                  '--source-snapshot',str(source/'source-snapshot.json'),'--container','fixture','--eval-dump','off','--execute']
            with patch('sys.argv',argv),patch('build_uroad_eval.validate_source_snapshot',return_value=proof),patch('build_uroad_eval.run_capture',return_value=json.dumps([{'Mounts':[{'Source':str(source),'Destination':'/root/workspace/apps'}]}])),patch('build_uroad_eval.subprocess.run',side_effect=invoke):
                self.assertEqual(0,main())
            receipt=json.loads((Path(temp)/'out/artifact/build-manifest.json').read_text())
            self.assertEqual('build.sh',receipt['build_mode']);self.assertFalse(receipt['effective_eval_dump'])

    def test_credential_launcher_redacts_output_and_checks_git_scope(self):
        import contextlib
        secret={'username':'fixture','password':'fixture-password','gitlab_token':'fixture-token'}
        child=Mock(stdout=io.BytesIO(b'error: fixture-password fixture-token\n'));child.wait.return_value=0
        output=io.StringIO()
        with patch('pathlib.Path.read_text',return_value=json.dumps(secret)),patch('subprocess.Popen',return_value=child) as popen,patch('sys.argv',['launch','./build.sh']),contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as e:exec(compile(remote.LAUNCHER,'launch','exec'),{})
        self.assertEqual(0,e.exception.code)
        self.assertEqual('error: [REDACTED] [REDACTED]\n',output.getvalue())
        self.assertEqual('fixture-token',popen.call_args.kwargs['env']['GITLAB_ACCESS_TOKEN'])
        output=io.StringIO()
        with patch('sys.stdin',io.StringIO('protocol=https\nhost=untrusted.example\npath=liconan/conan-config.git\n')),patch('sys.argv',['helper','get']),patch('pathlib.Path.read_text') as read,contextlib.redirect_stdout(output):
            exec(compile(remote.GIT_CREDENTIAL,'helper','exec'),{})
        self.assertEqual('',output.getvalue());read.assert_not_called()

    def test_cleanup_removes_only_this_attempt_auth_and_conan_token_db(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'transient-auth').mkdir();(root/'transient-auth/credentials.json').write_text('fixture')
            (root/'conan-home').mkdir();(root/'conan-home/.conan.db').write_text('fixture-token-cache')
            (root/'conan-home/metadata.json').write_text('keep')
            self.assertTrue(remote.clear_private_auth(root))
            self.assertFalse((root/'transient-auth').exists());self.assertFalse((root/'conan-home/.conan.db').exists())
            self.assertEqual('keep',(root/'conan-home/metadata.json').read_text())


if __name__=='__main__':unittest.main()
