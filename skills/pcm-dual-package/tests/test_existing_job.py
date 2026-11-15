import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import pathlib
import tempfile
import sys
import tarfile
import unittest
import types
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import pcm_existing_job as job
from prepare_pcm_components import prepare,recipe
from resolve_pcm_input import resolve


@contextlib.contextmanager
def workspace():
    with tempfile.TemporaryDirectory(prefix='pcm-test-') as folder:
        yield pathlib.Path(folder)


def context(published=True):
    pair='synthetic-pair-001'
    return {'pair_id':pair,'source':{'repository':job.UROAD,'branch':'dev-314','commit':'1'*40},
        'baseline_lock_sha256':'2'*64,
        'common_parameters':{'CONTROLLER':'CCU_M100_AVMLIVC','BASE_LINE':'0075','SOFTWARE_BRANCH':'0111','BUILDVARIANT':'release',
            'MANIFEST_NAME':'successful.lock','BUILD_REPLAY':True,'UPLOAD_ALL_PACKAGE':False,'SILENT_BUILD':False,'NOTIFY_CHAT_ID':''},
        'common_app_refs':['livc_integrate/0.2.11-avm@livc/stable'],
        'common_locked_refs':['xlidar/0.2.8-pcm.synthetic@livc/pcm-'+pair],
        'common_recipe_evidence':{'xlidar/0.2.8-pcm.synthetic@livc/pcm-'+pair:{'availability':'remote_recipe','recipe_revision':'f'*32,'recipe_sha256':'4'*64}} if published else {},
        'components':{mode:{'ref':'uroad_m100/3.1.4-pcm-'+mode.lower()+'.synthetic@livc/pcm-'+pair,
            'source_commit':'1'*40,'cmake':{'WITH_EVAL_DUMP':mode},'recipe_revision':('a' if mode=='OFF' else 'b')*32,
            'recipe_sha256':'3'*64,'availability':'remote_recipe' if published else 'local_recipe'} for mode in ['OFF','ON']}}


def locks():
    refs=context()['components']
    result={}
    for mode in ['OFF','ON']:
        result[mode]={'version':'0.4','profile_host':'host','profile_build':'build','graph_lock':{'nodes':{
            '0':{'ref':'Livc/0075','requires':['1']},
            '1':{'ref':refs[mode]['ref']+'#'+refs[mode]['recipe_revision'],'package_id':('1' if mode=='OFF' else '2')*40,'prev':'3'*32,'requires':['2','3'],'options':''},
            '2':{'ref':'infer_expert_sdk/0.1.89-rc2@livc/stable#'+'c'*32,'package_id':'d'*40,'prev':'e'*32,'options':'shared=True','requires':['3']},
            '3':{'ref':'xlidar/0.2.8-pcm.synthetic@livc/pcm-synthetic-pair-001#'+'f'*32,'package_id':'4'*40,'prev':'5'*32,'options':'with_replay=True'}}}}
    return result


def archive(path,mode,lock_name,lock_data,changes=None):
    values={'livc/lib/liburoad_m100.so':('M100-'+mode).encode(),'livc/lib/libinferfast.so':b'common-sdk',
            'livc/lib/libxlidar.so':b'common-lidar','livc/etc/model.bin':b'common-model','livc/etc/VERSION':mode.encode(),
            'livc/'+lock_name:lock_data}
    values.update(changes or {})
    with tarfile.open(path,'w:gz') as out:
        for name,data in values.items():
            info=tarfile.TarInfo(name); info.mode=0o644; info.size=len(data)
            out.addfile(info,io.BytesIO(data))
        info=tarfile.TarInfo('livc/lib/alias.so'); info.type=tarfile.SYMTYPE; info.linkname='libinferfast.so'
        out.addfile(info)


def start_pair(state):
    for mode,number in [('OFF',101),('ON',102)]:
        job.submit_intent(state,mode,state['plan_sha256'])
        job.record_build(state,mode,job.JOB_URL+str(number)+'/')


class ExistingJobTests(unittest.TestCase):
    def test_only_component_reference_changes_in_original_parameters(self):
        state=job.make_plan(context())
        off=state['builds']['OFF']['parameters']; on=state['builds']['ON']['parameters']
        self.assertEqual([k for k in off if off[k]!=on[k]],['APP_VERSION'])
        self.assertFalse(any(k.startswith('PCM_') for k in off))
        self.assertFalse(off['UPLOAD_ALL_PACKAGE'])

    def test_local_recipe_does_not_authorize_submission(self):
        state=job.make_plan(context(False))
        self.assertEqual(state['status'],'COMPONENT_RECIPES_REQUIRED')
        with self.assertRaises(ValueError): job.submit_intent(state,'OFF',state['plan_sha256'])

    def test_missing_private_common_dependency_blocks_submission(self):
        data=context(); data['common_recipe_evidence']={}
        state=job.make_plan(data)
        self.assertEqual(state['status'],'COMPONENT_RECIPES_REQUIRED')
        self.assertEqual(len(state['pending_common_refs']),1)
        with self.assertRaises(ValueError): job.submit_intent(state,'OFF',state['plan_sha256'])

    def test_changed_final_parameters_require_new_review(self):
        state=job.make_plan(context()); state['builds']['ON']['parameters']['SILENT_BUILD']=True
        with self.assertRaises(ValueError): job.submit_intent(state,'OFF',state['plan_sha256'])

    def test_same_or_ambiguous_components_rejected(self):
        data=context(); data['components']['ON']=copy.deepcopy(data['components']['OFF'])
        with self.assertRaises(ValueError): job.make_plan(data)
        data=context(); data['common_locked_refs'].append('xlidar/0.2.8@livc/stable')
        with self.assertRaises(ValueError): job.make_plan(data)

    def test_revision_suffix_does_not_falsely_pin_original_job(self):
        data=context(); data['common_app_refs']=['some/1.0@livc/stable#'+'a'*32]
        with self.assertRaises(ValueError): job.make_plan(data)

    def test_submission_uncertainty_blocks_duplicate_click(self):
        state=job.make_plan(context()); job.submit_intent(state,'OFF',state['plan_sha256'])
        with self.assertRaises(ValueError): job.submit_intent(state,'OFF',state['plan_sha256'])

    def test_different_real_build_identities_required(self):
        state=job.make_plan(context()); start_pair(state)
        self.assertNotEqual(state['builds']['OFF']['build_url'],state['builds']['ON']['build_url'])
        with self.assertRaises(ValueError): job.record_build(state,'ON',job.JOB_URL+'101/')
        with self.assertRaises(ValueError): job.record_build(state,'ON','https://ci.ep.chehejia.com/job/OTHER/102/')

    def test_historical_artifact_cannot_be_bound_to_current_build(self):
        state=job.make_plan(context()); start_pair(state)
        result={'build_url':job.JOB_URL+'101/','result':'SUCCESS','app_url':job.ARTIFACT_ROOT+'Release/app/app-99.app.tar.gz',
                'lock_url':job.ARTIFACT_ROOT+'Release/app-99.lock','app_sha256':'1'*64,'lock_sha256':'2'*64}
        with self.assertRaises(ValueError): job.record_result(state,'OFF',result)

    def test_one_failed_job_never_becomes_pair_success(self):
        state=job.make_plan(context()); start_pair(state)
        job.record_result(state,'OFF',{'build_url':job.JOB_URL+'101/','result':'FAILURE'})
        job.record_result(state,'ON',{'build_url':job.JOB_URL+'102/','result':'SUCCESS',
            'app_url':job.ARTIFACT_ROOT+'Release/app/app-102.app.tar.gz','lock_url':job.ARTIFACT_ROOT+'Release/app-102.lock','app_sha256':'1'*64,'lock_sha256':'2'*64})
        self.assertEqual(state['status'],'PARTIAL_OR_FAILED')

    def test_original_standard_locks_compare_common_dependencies(self):
        pair=locks(); self.assertEqual(job.compare_locks(pair['OFF'],pair['ON'],context()['components'])['status'],'PASS')
        pair['ON']['graph_lock']['nodes']['2']['prev']='0'*32
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'])

    def test_common_recipe_revision_is_bound_even_if_both_jobs_drift_together(self):
        pair=locks()
        for mode in ['OFF','ON']:
            pair[mode]['graph_lock']['nodes']['3']['ref']=pair[mode]['graph_lock']['nodes']['3']['ref'].split('#')[0]+'#'+'0'*32
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'],context()['common_recipe_evidence'])

    def test_wrong_recipe_or_incomplete_binary_identity_rejected(self):
        pair=locks(); pair['ON']['graph_lock']['nodes']['1']['ref']=pair['OFF']['graph_lock']['nodes']['1']['ref']
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'])
        pair=locks(); pair['ON']['graph_lock']['nodes']['2'].pop('prev')
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'])

    def test_profiles_and_dependency_edges_are_compared(self):
        pair=locks(); pair['ON']['profile_host']='different'
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'])
        pair=locks(); pair['ON']['graph_lock']['nodes']['2']['requires']=[]
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'])

    def test_m100_non_dump_options_cannot_drift(self):
        pair=locks(); pair['ON']['graph_lock']['nodes']['1']['options']='with_vis=False'
        with self.assertRaises(ValueError): job.compare_locks(pair['OFF'],pair['ON'],context()['components'])

    def test_only_m100_and_exact_build_metadata_may_differ(self):
        with workspace() as work:
            off=work/'OFF.tgz'; on=work/'ON.tgz'
            archive(off,'OFF','app-101.lock',b'off-lock'); archive(on,'ON','app-102.lock',b'on-lock')
            result=job.compare_apps(off,on,'app-101.lock','app-102.lock')
            self.assertEqual(result['status'],'PASS')
            archive(on,'ON','app-102.lock',b'on-lock',{'livc/etc/model.bin':b'changed'})
            with self.assertRaises(ValueError): job.compare_apps(off,on,'app-101.lock','app-102.lock')

    def test_embedded_lock_must_match_external_standard_lock(self):
        with workspace() as work:
            off=work/'OFF.tgz'; on=work/'ON.tgz'
            archive(off,'OFF','app-101.lock',b'off-lock'); archive(on,'ON','app-102.lock',b'on-lock')
            with self.assertRaises(ValueError): job.compare_apps(off,on,'app-101.lock','app-102.lock',['1'*64,'2'*64])

    def test_archive_traversal_and_same_m100_binary_are_rejected(self):
        with workspace() as work:
            off=work/'OFF.tgz'; on=work/'ON.tgz'
            archive(off,'OFF','app-101.lock',b'off-lock',{'livc/../../escape':b'bad'})
            with self.assertRaises(ValueError): job.inventory(off)
            archive(off,'OFF','app-101.lock',b'off-lock'); archive(on,'ON','app-102.lock',b'on-lock',{'livc/lib/liburoad_m100.so':b'M100-OFF'})
            with self.assertRaises(ValueError): job.compare_apps(off,on,'app-101.lock','app-102.lock')

    def test_recipe_preparation_is_isolated_and_pins_cmake_without_new_option(self):
        source='''import semver
class Component:
    name = "uroad_m100"
    branch = "master"
    options = {"shared": [True, False]}
    def init(self): self.v = semver.semver("0.1.0", False)
    def requirements(self): self.requires("xlidar/0.2.8@livc/stable")
    def _configure_cmake(self):
        self._cmake.configure(source_folder=self.source_folder)
'''
        changed=recipe(source,'uroad_m100','3.1.4-pcm-off.test','1'*40,'OFF','xlidar/0.2.8@livc/pcm-test')
        self.assertIn("['WITH_EVAL_DUMP'] = 'OFF'",changed)
        self.assertIn("branch = '"+'1'*40+"'",changed)
        self.assertNotIn('with_eval_dump',changed)
        with self.assertRaises(ValueError): recipe(changed,'uroad_m100','3.1.4','1'*40,'ON')
        ast.parse(changed)

    def test_duplicate_json_fields_are_rejected(self):
        with workspace() as work:
            p=work/'duplicate.json'; p.write_text('{"value":1,"value":2}')
            with self.assertRaises(ValueError): job.load(p)

    def test_full_recipe_bundle_uses_resolved_lidar_version_and_private_channel(self):
        common='''import semver
class Component:
    name = NAME
    branch = "master"
    def init(self): self.v = semver.semver("0.1.0", False)
    def requirements(self): self.requires("xlidar/0.2.8@livc/stable")
    def _configure_cmake(self):
        BINDING
        self._cmake.configure(source_folder=self.source_folder)
'''
        with workspace() as work:
            uroad=work/'uroad'; lidar=work/'lidar'; uroad.mkdir(); lidar.mkdir()
            (uroad/'conanfile.py').write_text(common.replace('NAME','"uroad_m100"').replace('BINDING','pass'))
            (lidar/'conanfile.py').write_text(common.replace('NAME','"xlidar"').replace('BINDING','self._cmake.definitions["WITH_REPLAY"] = False'))
            result=prepare(uroad,lidar,'1'*40,'2'*40,'4.0.0','fixture-pair-001',work/'output',lidar_version_base='1.2.3')
            self.assertFalse(result['published'])
            self.assertIn('xlidar/1.2.3-pcm.',result['components']['xlidar']['ref'])
            for mode in ['OFF','ON']:
                self.assertEqual(result['components'][mode]['source_commit'],'1'*40)
                self.assertTrue(result['components'][mode]['ref'].endswith('@livc/pcm-fixture-pair-001'))
            with self.assertRaises(ValueError): prepare(uroad,lidar,'1'*40,'2'*40,'4.0.0','fixture-pair-002',work/'missing-version')

    def test_private_recipe_fetches_at_source_root_without_secret_in_command(self):
        original='''import semver
class Component:
    name = "uroad_m100"
    branch = "master"
    def init(self): self.v = semver.semver("0.1.0", False)
    def source(self):
        self.run("git clone https://gitlab.chehejia.com/livc-acore/chassis/uroad.git")
    def _configure_cmake(self):
        self._cmake.configure(source_folder=self.source_folder)
'''
        changed=recipe(original,'uroad_m100','3.1.4-pcm-off.test','1'*40,'OFF')
        source=next(n for n in ast.walk(ast.parse(changed)) if isinstance(n,ast.FunctionDef) and n.name=='source')
        namespace={}; captured=[]
        @contextlib.contextmanager
        def environment(values):
            with patch.dict(os.environ,values): yield
        namespace['tools']=types.SimpleNamespace(get_env=lambda key:'SYNTHETIC_ONLY',environment_append=environment)
        exec(compile(ast.Module(body=[source],type_ignores=[]),'<private-recipe>','exec'),namespace)
        with workspace() as work:
            @contextlib.contextmanager
            def temporary(**kwargs):
                directory=work/'helper'; directory.mkdir(); yield str(directory)
            def run(text):
                captured.append(text)
                helper=pathlib.Path(os.environ['GIT_ASKPASS'])
                self.assertTrue(helper.is_file())
                self.assertNotIn('SYNTHETIC_ONLY',helper.read_text())
                self.assertNotIn('SYNTHETIC_ONLY',text)
            with patch('tempfile.TemporaryDirectory',temporary):
                namespace['source'](types.SimpleNamespace(branch='1'*40,run=run))
        self.assertEqual(captured[0],'git init')
        self.assertTrue(captured[1].endswith('uroad.git '+'1'*40))
        self.assertEqual(captured[2],'git checkout FETCH_HEAD')
        self.assertNotIn('cd uroad','\n'.join(captured))

    def test_complete_cli_pair_requires_two_uploads_and_both_comparisons(self):
        with workspace() as work:
            state=job.make_plan(context()); start_pair(state); pair=locks(); files={}
            for mode,number in [('OFF',101),('ON',102)]:
                lock=work/('app-'+str(number)+'.lock'); lock.write_bytes(job.canonical(pair[mode]))
                app=work/('app-'+str(number)+'.app.tar.gz'); archive(app,mode,lock.name,lock.read_bytes())
                result={'build_url':job.JOB_URL+str(number)+'/','result':'SUCCESS','app_url':job.ARTIFACT_ROOT+'Release/app/'+app.name,
                    'lock_url':job.ARTIFACT_ROOT+'Release/'+lock.name,'app_sha256':job.sha_file(app),'lock_sha256':job.sha_file(lock)}
                job.record_result(state,mode,result); files[mode]=(app,lock)
            path=work/'pair.json'; path.write_bytes(job.canonical(state))
            cmd=[str(pathlib.Path(job.__file__)),'compare','--state',str(path)]
            for mode in ['OFF','ON']:
                for kind,file in zip(['app','lock'],files[mode]): cmd+=['--'+mode.lower()+'-'+kind,str(file)]
            output=io.StringIO()
            with patch.object(sys,'argv',cmd),contextlib.redirect_stdout(output):
                job.main()
            self.assertEqual(json.loads(output.getvalue())['status'],'COMPLETE')
            actual=job.load(path); self.assertEqual(actual['status'],'COMPLETE')
            text=job.summary(actual); self.assertEqual(text.count('LIVC APP：'),2); self.assertEqual(text.count('构建链接：'),2)


class InputContractTests(unittest.TestCase):
    def test_branch_link_resolves_repository_and_branch(self):
        result=resolve('https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/dev-314')
        self.assertEqual((result['repository'],result['branch'],result['status']),(job.UROAD,'dev-314','READY_TO_PIN_COMMIT'))

    def test_repository_name_uses_only_matching_known_branch_context(self):
        result=resolve('uroad',context_repository=job.UROAD,context_branch='dev-314')
        self.assertEqual(result['branch'],'dev-314'); self.assertTrue(result['branch_from_existing_context'])

    def test_repository_home_does_not_silently_choose_master(self):
        result=resolve(job.UROAD[:-4])
        self.assertEqual(result['status'],'NEEDS_BRANCH'); self.assertIsNone(result['branch'])

    def test_explicit_branch_with_repository_or_ssh_url(self):
        for value in ['livc-acore/chassis/uroad','git@gitlab.chehejia.com:livc-acore/chassis/uroad.git']:
            self.assertEqual(resolve(value,kind='repository',explicit_branch='dev-314')['branch'],'dev-314')

    def test_branch_name_requires_a_repository_context(self):
        self.assertEqual(resolve('dev-314',kind='branch')['status'],'NEEDS_REPOSITORY')
        self.assertEqual(resolve('dev-314',kind='branch',context_repository=job.UROAD)['status'],'READY_TO_PIN_COMMIT')

    def test_other_repository_cannot_reuse_uroad_branch_or_job(self):
        result=resolve('https://gitlab.chehejia.com/team/other',context_repository=job.UROAD,context_branch='dev-314')
        self.assertEqual(result['status'],'NEEDS_BRANCH'); self.assertIsNone(result['branch'])
        result=resolve('team/other',kind='repository',explicit_branch='dev-314')
        self.assertEqual(result['status'],'REPOSITORY_COMPATIBILITY_CHECK_REQUIRED')

    def test_display_name_can_request_gitlab_lookup(self):
        result=resolve('预瞄项目',kind='repository',explicit_branch='dev-314')
        self.assertEqual(result['status'],'NEEDS_REPOSITORY_LOOKUP'); self.assertEqual(result['branch'],'dev-314')

    def test_branch_slashes_and_normal_gitlab_query_are_preserved(self):
        result=resolve('https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/feature%2Ftest?ref_type=heads')
        self.assertEqual(result['branch'],'feature/test')

    def test_conflicting_or_credential_containing_inputs_are_rejected(self):
        with self.assertRaises(ValueError): resolve('https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/dev-314',explicit_branch='other')
        for value in ['https://user:secret@gitlab.chehejia.com/team/repo','https://other.example/team/repo','https://gitlab.chehejia.com/team/repo?token=secret']:
            with self.subTest(value=value),self.assertRaises(ValueError): resolve(value)


class OriginalNotificationRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data={}
        fixture=pathlib.Path(__file__).parent/'fixtures/notification_target_fixture.py'
        exec(compile(fixture.read_text(),str(fixture),'exec'),data)
        cls.routing=staticmethod(data['resolve_notification_target'])

    def client(self,builder=True,target=True):
        return types.SimpleNamespace(lookup_user_id=lambda value:'builder-id' if builder else None,resolve_user_id=lambda value:'target-id' if target else None)

    def test_real_builder_is_mentioned_and_custom_target_ignored(self):
        self.assertEqual(self.routing(self.client(),'synthetic-builder','custom-user','custom-chat'),('builder-id',False))

    def test_agent_delegation_is_additional_only_when_both_fields_exist(self):
        self.assertEqual(self.routing(self.client(False),'','custom-user','custom-chat'),('target-id',True))

    def test_missing_or_unresolvable_user_does_not_claim_a_mention(self):
        self.assertEqual(self.routing(self.client(False,False),'','custom-user','custom-chat'),('',False))

    def test_a_chat_id_alone_does_not_replace_the_default_destination(self):
        self.assertEqual(self.routing(self.client(False),'','','custom-chat'),('',False))


if __name__=='__main__': unittest.main()
