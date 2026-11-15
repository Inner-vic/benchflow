"""No CI, publication, gateway state, hardware or network is used by these tests."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pcm_workflow as workflow
import pcm_existing_job as legacy


def context(delivery='single_app_with_variants', published=True):
    commit='1'*40
    result={'source':{'repository':workflow.UROAD,'branch':'feature/example','commit':commit},
        'compile_modes':['OFF','ON'],'delivery':delivery,'base_app_mode':'OFF',
        'base_app_mode_evidence':{'mode':'OFF','source_commit':commit,'origin':'verified_source_default'},
        'baseline_lock_sha256':'2'*64,
        'baseline_artifact_checks':{'verified':True,'liware_present':False,'limesh_present':False},
        'common_parameters':{'CONTROLLER':'CCU_M100_AVMLIVC','BASE_LINE':'008D','SOFTWARE_BRANCH':'0111',
            'BUILDVARIANT':'release','MANIFEST_NAME':'synthetic-baseline.lock','BUILD_REPLAY':True,
            'UPLOAD_ALL_PACKAGE':False,'SILENT_BUILD':False,'LIWARE_URL':'','LIMESH_URL':'',
            'PORTAL_TRIGGER_URL':'','PORTAL_RESOURCE_KEY':''},
        'common_app_refs':['conan_shared/1.0.0@livc/release-schu'],'common_locked_refs':[],
        'components':{},'compile_evidence':{}}
    for mode in ('OFF','ON'):
        ref='uroad_m100/1.0.0-'+mode.lower()+'@livc/pcm-synthetic'
        result['components'][mode]={'ref':ref,'source_commit':commit,'cmake':{'WITH_EVAL_DUMP':mode},
            'recipe_sha256':'3'*64,'recipe_revision':'a'*32,
            'availability':'remote_recipe' if published else 'local_recipe'}
        result['compile_evidence'][mode]={'state':'BUILT','source_commit':commit,'component':ref,
            'recipe_sha256':'3'*64,'effective_eval_dump':mode,'machine':'AArch64',
            'output_sha256':('4' if mode=='OFF' else '5')*64,'output_bytes':100}
    return result


class WorkflowTests(unittest.TestCase):
    def test_one_app_two_components_is_one_jenkins_action_and_zero_bench_jobs(self):
        value=workflow.make_plan(context())
        self.assertEqual(value['state'],'READY_FOR_APP_REVIEW')
        self.assertEqual(value['compile_modes'],['OFF','ON'])
        self.assertEqual(value['app_count'],1)
        self.assertEqual(value['jenkins_modes'],['OFF'])
        self.assertEqual(value['bench_task_count'],0)
        self.assertTrue(value['bench_execution_requires_material_confirmation'])

    def test_component_only_needs_no_jenkins_configuration_or_publication(self):
        value=context('components_only',False)
        for key in ('common_parameters','common_app_refs','common_locked_refs','baseline_lock_sha256',
                    'base_app_mode','base_app_mode_evidence','baseline_artifact_checks'):
            value.pop(key)
        plan=workflow.make_plan(value)
        self.assertEqual(plan['state'],'COMPONENTS_COMPILED')
        self.assertEqual(plan['jenkins_modes'],[])
        self.assertEqual(plan['app_count'],0)

    def test_explicit_two_app_delivery_remains_available(self):
        value=workflow.make_plan(context('app_pair'))
        self.assertEqual(value['jenkins_modes'],['OFF','ON'])
        self.assertEqual(value['app_count'],2)

    def test_unknown_delivery_never_silently_becomes_two_apps(self):
        for delivery in (None,'','both','guess'):
            with self.subTest(delivery=delivery),self.assertRaises(ValueError):
                workflow.make_plan(context(delivery))

    def test_legacy_pair_helper_rejects_new_single_app_requests(self):
        with self.assertRaises(ValueError):legacy.make_plan(context())

    def test_goal_never_flips_compile_mode_or_app_mode(self):
        for goal in ('replay','replay_and_evaluate'):
            value=context();value['goal']=goal
            plan=workflow.make_plan(value)
            self.assertEqual(plan['requested_app_modes'],['OFF'])
            self.assertEqual(plan['components']['OFF']['cmake'],{'WITH_EVAL_DUMP':'OFF'})

    def test_unpublished_app_component_blocks_jenkins_but_not_completed_so_evidence(self):
        value=workflow.make_plan(context(published=False))
        self.assertEqual(value['state'],'COMPONENT_PUBLICATION_REQUIRED')
        self.assertEqual(value['compiled_modes'],['OFF','ON'])
        self.assertEqual(value['jenkins_modes'],[])
        with self.assertRaises(ValueError):
            workflow.require_app_review(value,'OFF',value['plan_sha256'])

    def test_unused_on_recipe_need_not_be_published_for_single_off_app(self):
        value=context();value['components']['ON']['availability']='local_recipe'
        self.assertEqual(workflow.make_plan(value)['jenkins_modes'],['OFF'])

    def test_publish_both_recipes_does_not_mean_build_both_apps(self):
        value=context();value['publish_modes']=['OFF','ON']
        plan=workflow.make_plan(value)
        self.assertEqual(plan['publication_modes'],['OFF','ON'])
        self.assertEqual(plan['jenkins_modes'],['OFF'])
        value['components']['ON']['availability']='local_recipe'
        plan=workflow.make_plan(value)
        self.assertEqual(plan['missing_remote_modes'],['ON'])
        self.assertEqual(plan['jenkins_modes'],[])

    def test_component_publication_can_be_requested_without_app_build(self):
        value=context('components_only',False);value['publish_modes']=['OFF','ON']
        plan=workflow.make_plan(value)
        self.assertEqual(plan['state'],'COMPONENT_PUBLICATION_REQUIRED')
        self.assertEqual(plan['requested_app_modes'],[])
        self.assertEqual(plan['app_count'],0)

    def test_publication_scope_cannot_add_unrequested_or_duplicate_variants(self):
        for modes in (['unknown'],['OFF','OFF']):
            value=context();value['publish_modes']=modes
            with self.subTest(modes=modes),self.assertRaises(ValueError):workflow.make_plan(value)

    def test_missing_or_mismatched_compile_proof_is_not_accepted(self):
        value=context();value['compile_evidence'].pop('ON')
        self.assertEqual(workflow.make_plan(value)['state'],'COMPILATION_REQUIRED')
        for key,bad in [('source_commit','f'*40),('recipe_sha256','f'*64),('effective_eval_dump','OFF'),
                        ('output_bytes',True),('machine','x86_64')]:
            value=context();value['compile_evidence']['ON'][key]=bad
            with self.subTest(key=key),self.assertRaises(ValueError):workflow.make_plan(value)

    def test_base_mode_must_have_provenance(self):
        value=context();value.pop('base_app_mode_evidence')
        with self.assertRaises(ValueError):workflow.make_plan(value)

    def test_inherited_image_is_not_ignored_when_url_fields_are_empty(self):
        value=context();value['baseline_artifact_checks']['liware_present']=True
        plan=workflow.make_plan(value)
        self.assertEqual(plan['state'],'BASELINE_PROPERTIES_REQUIRED')
        self.assertEqual(plan['jenkins_modes'],[])

    def test_missing_shared_recipe_blocks_app_even_when_m100_is_published(self):
        value=context();value['common_locked_refs']=['xlidar/1.0.0@livc/pcm-test']
        plan=workflow.make_plan(value)
        self.assertEqual(plan['state'],'COMMON_RECIPES_REQUIRED')
        self.assertEqual(plan['jenkins_modes'],[])

    def test_review_hash_and_selected_app_mode_bound_submission_parameters(self):
        value=workflow.make_plan(context());sha=value['plan_sha256']
        self.assertIn('uroad_m100/1.0.0-off',workflow.require_app_review(value,'OFF',sha)['APP_VERSION'])
        with self.assertRaises(ValueError):workflow.require_app_review(value,'ON',sha)
        altered=copy.deepcopy(value);altered['parameters']['OFF']['BASE_LINE']='008E'
        with self.assertRaises(ValueError):workflow.require_app_review(altered,'OFF',sha)

    def test_off_cannot_claim_eval_and_on_does_not_guarantee_outputs(self):
        self.assertIn('EVAL_CAPABILITY_REQUIRED',workflow.evaluation_requirements('OFF',False,'replay_and_evaluate'))
        self.assertIn('EVAL_RUNTIME_CONFIGURATION_REQUIRED',workflow.evaluation_requirements('ON',False,'replay_and_evaluate'))
        self.assertIn('PREDICTION_OUTPUTS_NOT_VERIFIED',workflow.evaluation_requirements('ON',True,'replay_and_evaluate'))
        self.assertEqual(workflow.evaluation_requirements('ON',True,'replay_and_evaluate',outputs_verified=True),[])

    def test_both_modes_can_replay_without_forcing_runtime_or_metrics(self):
        self.assertEqual(workflow.evaluation_requirements('OFF',False,'replay'),[])
        self.assertEqual(workflow.evaluation_requirements('ON',False,'replay'),[])
        self.assertIn('COMPILE_RUNTIME_INCONSISTENT',workflow.evaluation_requirements('OFF',True,'replay'))

    def test_planning_does_not_mutate_the_original_context(self):
        value=context();before=copy.deepcopy(value)
        workflow.make_plan(value)
        self.assertEqual(value,before)


if __name__=='__main__':unittest.main()
