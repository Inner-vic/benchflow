import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import unittest

from bench_gateway.core import GatewayError,Store,within,remote_within,worker_lock,digest
from bench_gateway.service import Service,identity,validate_intent
from bench_gateway.worker import tick,deliver,postprocess_tick
from bench_gateway.transfer import download
from bench_gateway.parser import command
from bench_gateway.setup import cc_config

A='ou_test_alice';B='ou_test_bob'
SESSION='feishu:oc_alice:'+A


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg={'state_root':str(self.root/'state'),'project_root':str(self.root),'mode':'simulate','live_enabled':False,
                  'cc_project':'test','feishu_app_id':'cli_test','notifications_enabled':False,'remote_users_root':'/data/users'}
        self.service=Service(self.cfg);self.s=self.service.store
        self.s.allow(A);self.s.allow(B)
        self.package=self.s.add_asset(A,'package','A',{'sha256':'abc'})
        self.dataset=self.s.add_asset(A,'dataset','D',{'sha256':'def'})
        self.dependency=self.s.add_asset(A,'dependency','M100',{'sha256':'ghi'})
        self.ids={'package':self.package['id'],'dataset':self.dataset['id'],'dependencies':[self.dependency['id']]}
        self.s.add_baseline(A,'verified',{'gate_verified':True,'asset_ids':self.ids})
        self.intent={'action':'replay','package':'A','dataset':'D','dependencies':['M100'],'baseline':'verified','scenes':[1,3]}

    def tearDown(self):self.tmp.cleanup()

    def enqueue(self,text='执行回灌',request_id=None):
        self.service.handle(SESSION,text,self.intent,request_id)
        return self.s.jobs(A)[0]

    def test_same_asset_name_resolves_per_owner(self):
        self.s.add_asset(B,'package','A',{'sha256':'bob'})
        self.assertEqual(self.s.asset(A,'package','A')['data']['sha256'],'abc')
        self.assertEqual(self.s.asset(B,'package','A')['data']['sha256'],'bob')

    def test_missing_owned_asset_never_falls_back(self):
        self.s.add_asset(B,'package','B-only',{})
        with self.assertRaises(GatewayError):self.s.asset(A,'package','B-only')

    def test_identity_is_from_transport_not_text(self):
        self.assertEqual(identity(SESSION),A)
        with self.assertRaises(GatewayError):validate_intent({'action':'help','owner':B})

    def test_shared_and_thread_sessions_fail_closed(self):
        for s in ['feishu:oc_x','feishu:oc_x:root:om_x','telegram:1:2','feishu:oc_x:root']:
            with self.assertRaises(GatewayError):identity(s)

    def test_feishu_user_auto_provision(self):
        user='ou_new_user';self.s.provision(user)
        self.assertTrue(self.s.user_root(user).is_dir())
        self.assertEqual(self.s.list_assets(user),[])

    def test_provision_does_not_reenable_disabled_account(self):
        with self.s.connect() as c:c.execute('UPDATE users SET enabled=0 WHERE id=?',(A,))
        with self.assertRaises(GatewayError):self.s.provision(A)

    def test_context_contains_no_foreign_asset_or_paths(self):
        self.s.add_asset(B,'package','secret-bob',{'local_path':'secret/path'})
        context=json.dumps(self.service.context(A))
        self.assertNotIn('secret-bob',context);self.assertNotIn('local_path',context)
        self.assertIn('verified',context)

    def test_new_content_versions_are_immutable(self):
        new=self.s.add_asset(A,'package','A',{'sha256':'new'})
        self.assertEqual(new['version'],2)
        self.assertEqual(self.package['data']['sha256'],'abc')
        with self.assertRaises(GatewayError):self.enqueue()

    def test_no_configuration_asset_category(self):
        with self.assertRaises(GatewayError):self.s.add_asset(A,'configuration','config',{})

    def test_unvalidated_combination_is_not_submitted(self):
        self.s.add_baseline(A,'unverified',{'gate_verified':False,'asset_ids':self.ids})
        self.intent['baseline']='unverified'
        with self.assertRaises(GatewayError):self.enqueue()
        self.assertEqual(self.s.jobs(A),[])

    def test_replay_defaults_without_evaluation(self):
        j=self.enqueue();self.assertFalse(j['payload']['evaluate_after'])

    def test_unique_gate_is_selected_without_user_technical_id(self):
        self.intent.pop('baseline')
        j=self.enqueue();self.assertEqual(j['payload']['baseline']['name'],'verified')

    def test_queued_input_snapshot_is_immutable(self):
        j=self.enqueue();self.s.add_asset(A,'package','A',{'sha256':'later'})
        self.assertEqual(self.s.job(A,j['id'])['payload']['assets']['package']['data']['sha256'],'abc')

    def test_duplicate_request_survives_restart(self):
        j=self.enqueue();Service(self.cfg).handle(SESSION,'执行回灌',self.intent)
        self.assertEqual([x['id'] for x in self.s.jobs(A)],[j['id']])

    def test_concurrent_claims_are_serial(self):
        self.enqueue('run-1');self.enqueue('run-2')
        with concurrent.futures.ThreadPoolExecutor(2) as p:results=list(p.map(lambda _:self.s.claim(),range(2)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_cannot_query_or_cancel_foreign_job(self):
        j=self.enqueue()
        with self.assertRaises(GatewayError):self.s.job(B,j['id'])
        with self.assertRaises(GatewayError):self.s.cancel(B,j['id'])

    def test_can_cancel_queued_but_not_running(self):
        j=self.enqueue();self.s.cancel(A,j['id']);self.assertEqual(self.s.job(A,j['id'])['state'],'CANCELLED')
        j=self.enqueue('second');self.s.claim()
        with self.assertRaises(GatewayError):self.s.cancel(A,j['id'])

    def test_unknown_outcome_blocks_next_task(self):
        j=self.enqueue();j=self.s.claim();self.s.finish(j,'NEEDS_REVIEW',{'error':'disconnect'})
        self.enqueue('second');self.assertIsNone(self.s.claim())

    def test_simulation_never_claims_real_success(self):
        j=self.enqueue()
        class Fake:
            def execute(self,job):return {'simulated':True,'task':'SIMULATION/test'}
        tick(self.cfg,self.s,Fake());self.assertEqual(self.s.job(A,j['id'])['state'],'SIMULATED')

    def test_network_loss_is_not_replayed(self):
        j=self.enqueue()
        class Lost:
            def execute(self,job):raise ConnectionError('lost')
        tick(self.cfg,self.s,Lost());self.assertEqual(self.s.job(A,j['id'])['state'],'NEEDS_REVIEW')
        self.assertIsNone(tick(self.cfg,self.s,Lost()))

    def test_busy_workstation_waits_without_second_submission(self):
        j=self.enqueue()
        class Busy:
            def execute(self,job):return {'state':'RESOURCE_BUSY'}
        tick(self.cfg,self.s,Busy());self.assertEqual(self.s.job(A,j['id'])['state'],'QUEUED')

    def test_evaluate_uses_owned_existing_result(self):
        self.s.add_baseline(A,'evaluated',{'gate_verified':True,'asset_ids':self.ids,
                                         'evaluator':{'argv':['test-evaluator-must-not-run']}})
        self.intent['baseline']='evaluated'
        j=self.enqueue();self.s.finish(j,'SUCCEEDED',{'task':'/workstation/tasks/'+j['id']})
        self.service.handle(SESSION,'评测上次结果',{'action':'evaluate','task_id':j['id']})
        evaluation=self.s.jobs(A)[0]
        self.assertEqual(evaluation['payload']['action'],'evaluate')
        self.assertEqual(evaluation['payload']['source_job'],j['id'])

    def test_notification_failure_does_not_rerun_job(self):
        j=self.enqueue();self.s.finish(j,'SUCCEEDED',{'task':'/tasks/'+j['id']})
        self.cfg['notifications_enabled']=True
        self.assertEqual(deliver(self.cfg,self.s,lambda _:False),0)
        self.assertEqual(self.s.job(A,j['id'])['state'],'SUCCEEDED')
        self.assertEqual(deliver(self.cfg,self.s,lambda _:True),1)
        self.assertEqual(deliver(self.cfg,self.s,lambda _:True),0)

    def test_evaluation_failure_preserves_successful_replay(self):
        self.s.add_baseline(A,'evaluated',{'gate_verified':True,'asset_ids':self.ids,
                                         'evaluator':{'argv':['test-evaluator-must-not-run']}})
        self.intent['baseline']='evaluated'
        self.intent['evaluate_after']=True;j=self.enqueue()
        class Partial:
            def execute(self,job):
                if job['payload']['action']=='evaluate':raise GatewayError('auth expired')
                return {'task':'/tasks/'+job['id']}
        tick(self.cfg,self.s,Partial())
        self.assertEqual(self.s.job(A,j['id'])['state'],'POSTPROCESSING')
        postprocess_tick(self.cfg,self.s,Partial())
        result=self.s.job(A,j['id'])
        self.assertEqual(result['state'],'PARTIAL_SUCCESS')
        self.assertEqual(result['result']['task'],'/tasks/'+j['id'])

    def test_worker_restart_quarantines_unfinished_task(self):
        from bench_gateway.worker import run
        j=self.enqueue();self.s.claim()
        run(self.cfg,self.s,once=True)
        self.assertEqual(self.s.job(A,j['id'])['state'],'NEEDS_REVIEW')

    def test_local_and_remote_path_escape_rejected(self):
        for path in [self.root/'outside',self.s.user_root(B)/'file']:
            with self.assertRaises(GatewayError):within(self.s.user_root(A),path)
        for path in ['/etc/passwd','/data/users/alice/../bob/file']:
            with self.assertRaises(GatewayError):remote_within('/data/users/alice',path)

    def test_import_does_not_read_foreign_asset_directory(self):
        p=self.s.user_root(B)/'packages/secret.bin';p.write_bytes(b'bob')
        with self.assertRaises(GatewayError):self.s.import_file(A,'package','copied',p)

    def test_import_is_verified_and_repeat_is_idempotent(self):
        p=self.root/'new.so';p.write_bytes(b'library')
        a=self.s.import_file(A,'dependency','D',p,'lib/test.so')
        b=self.s.import_file(A,'dependency','D',p,'lib/test.so')
        self.assertEqual(a['id'],b['id']);self.assertEqual(a['data']['sha256'],digest(p))

    def test_symlink_escape_rejected(self):
        p=self.s.user_root(A)/'inbox/link'
        try:p.symlink_to(self.s.user_root(B),target_is_directory=True)
        except OSError:self.skipTest('host does not allow symlink creation')
        with self.assertRaises(GatewayError):within(self.s.user_root(A),p/'x')

    def test_urls_require_explicit_host_and_https(self):
        for u in ['http://example.com/a','file:///etc/passwd','https://example.com/a','https://u:p@example.com/a']:
            with self.assertRaises(GatewayError):download(self.cfg,self.s,A,u)

    def test_command_rejects_malformed_scene_selection(self):
        for scenes in [[True],[0],[2,1],[1,1],['1']]:
            with self.assertRaises(GatewayError):validate_intent({'action':'replay','scenes':scenes})

    def test_model_has_no_host_tools_or_user_config(self):
        argv=command({},'schema','out','cwd')
        self.assertIn('--ignore-user-config',argv);self.assertIn('--ephemeral',argv)
        self.assertIn('shell_tool',argv);self.assertIn('apps',argv)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',argv)

    def test_cc_configuration_does_not_contain_secret(self):
        (self.root/'config').mkdir();self.cfg['cc_binary']='cc-connect'
        text=cc_config(self.cfg).read_text(encoding='utf-8')
        self.assertIn('${BENCH_FEISHU_APP_SECRET}',text)
        self.assertIn('share_session_in_channel = false',text)
        self.assertIn('disabled_commands = ["*"]',text)

    def test_worker_lock_prevents_second_worker(self):
        with worker_lock(self.s.root):
            with self.assertRaises(GatewayError):
                with worker_lock(self.s.root):pass


if __name__=='__main__':unittest.main()
