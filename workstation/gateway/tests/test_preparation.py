import concurrent.futures
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError,Store
from bench_gateway.intake import Intake
from bench_gateway.preparation import MaterialCheck,PreparationQueue,scene_selection
from bench_gateway.queue_status import QueueStatus
from bench_gateway.worker import deliver,run


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state');self.user='ou_prep_alice';self.other='ou_prep_bob'
        for user in (self.user,self.other):self.store.allow(user)
        self.cfg={'state_root':str(self.root/'state'),'project_root':str(self.root),
                  'mode':'simulate','live_enabled':False,'notifications_enabled':False,
                  'remote_users_root':'/data/users','poll_seconds':.01,'notification_retry_seconds':5}
        self.queue=PreparationQueue(self.cfg,self.store);self.session='feishu:oc_alice:'+self.user
        Intake(self.store).update(self.user,{'package':'package-A','dataset':'共享18场景','scene_scope':'全部'})

    def tearDown(self):self.tmp.cleanup()

    def submit(self):
        self.queue.submit(self.user,self.session)
        return self.queue.latest(self.user)

    @staticmethod
    def checker():
        return SimpleNamespace(run=lambda job:{'status':'STATIC_PASSED','checks':[{'field':'package','status':'PASS'}]})

    def test_preparation_ignores_bench_disconnect_and_live_gate_without_creating_hardware_job(self):
        QueueStatus(self.store).resource({'state':'BENCH_UNREACHABLE','checked_at':time.time()})
        self.submit()
        with patch('bench_gateway.availability.probe',side_effect=AssertionError('must not probe hardware')):
            self.assertIsNotNone(self.queue.tick(self.checker()))
        self.assertEqual(self.queue.latest(self.user)['state'],'STATIC_PASSED')
        self.assertEqual(self.store.jobs(self.user),[])
        self.assertIn('未进入台架队列',self.queue.summary(self.user))
        self.assertIn('静态通过不代表运行兼容或已回放',self.queue.summary(self.user))

    def test_submitted_draft_is_an_immutable_snapshot(self):
        submitted=self.submit()
        Intake(self.store).update(self.user,{'scene_scope':'1'})
        seen=[]
        checker=SimpleNamespace(run=lambda job:seen.append(job['payload']) or {'status':'STATIC_PASSED'})
        self.queue.tick(checker)
        self.assertEqual(seen[0]['package'],'package-A')
        self.assertEqual(seen[0]['scene_scope'],'全部')
        self.assertEqual(self.queue.latest(self.user)['fingerprint'],submitted['fingerprint'])
        self.assertIn('草稿已变化',self.queue.summary(self.user))

    def test_private_drafts_and_latest_results_do_not_cross_users(self):
        self.submit()
        self.assertIsNone(self.queue.latest(self.other))
        self.assertNotIn('package-A',self.queue.summary(self.other))
        Intake(self.store).update(self.other,{'package':'private-bob','dataset':'bob-dataset'})
        self.queue.submit(self.other,'feishu:oc_bob:'+self.other)
        self.assertEqual(self.queue.latest(self.other)['payload']['package'],'private-bob')
        self.assertEqual(self.queue.latest(self.user)['payload']['package'],'package-A')

    def test_explicit_foreign_result_is_rejected(self):
        job=self.submit()
        with self.assertRaises(GatewayError):self.queue.summary(self.other,job)

    def test_concurrent_submissions_deduplicate_active_checks(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            replies=list(pool.map(lambda _:self.queue.submit(self.user,self.session),range(8)))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM preparations').fetchone()[0],1)
        self.assertEqual(sum('已受理' in reply for reply in replies),1)

    def test_concurrent_ticks_only_claim_one_active_check(self):
        self.submit();entered=threading.Event();release=threading.Event();calls=[]
        def check(job):
            calls.append(job['id']);entered.set()
            if not release.wait(5):raise AssertionError('test check was not released')
            return {'status':'STATIC_PASSED'}
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            future=pool.submit(self.queue.tick,SimpleNamespace(run=check))
            try:
                self.assertTrue(entered.wait(3))
                self.assertIsNone(self.queue.tick(self.checker()))
            finally:release.set()
            self.assertIsNotNone(future.result(timeout=5))
        self.assertEqual(len(calls),1)

    def test_restart_preserves_input_and_requires_explicit_retry(self):
        job=self.submit()
        with self.store.connect() as c:c.execute("UPDATE preparations SET state='RUNNING' WHERE id=?",(job['id'],))
        restarted=PreparationQueue(self.cfg,self.store);restarted.recover()
        saved=restarted.latest(self.user)
        self.assertEqual(saved['state'],'NEEDS_INPUT')
        self.assertEqual(saved['payload'],job['payload'])
        self.assertIn('不会自动重复下载',restarted.summary(self.user))
        with patch('bench_gateway.preparation.MaterialCheck.run') as check:
            self.assertIsNone(restarted.tick())
        check.assert_not_called()
        restarted.submit(self.user,self.session)
        self.assertNotEqual(restarted.latest(self.user)['id'],job['id'])

    def test_notification_retry_never_repeats_material_check(self):
        self.submit();calls=[]
        self.queue.tick(SimpleNamespace(run=lambda job:calls.append(job['id']) or {'status':'STATIC_PASSED'}))
        self.cfg['notifications_enabled']=True
        with patch('bench_gateway.preparation.time.time',return_value=100):
            self.assertEqual(self.queue.deliver(lambda row:False),0)
            self.assertEqual(self.queue.deliver(lambda row:True),0)
        sent=[]
        with patch('bench_gateway.preparation.time.time',return_value=105):
            self.assertEqual(deliver(self.cfg,self.store,lambda row:sent.append(row) or True),1)
            self.assertEqual(deliver(self.cfg,self.store,lambda row:True),0)
        self.assertEqual(len(calls),1)
        self.assertEqual(sent[0]['session'],self.session)
        self.assertIsNone(sent[0]['attachment'])
        self.assertEqual(self.store.jobs(self.user),[])

    def test_checker_failure_stops_once_and_reports_actionable_input(self):
        self.submit()
        def check(job):raise ConnectionError('private token must not be displayed')
        self.queue.tick(SimpleNamespace(run=check))
        summary=self.queue.summary(self.user)
        self.assertEqual(self.queue.latest(self.user)['state'],'NEEDS_INPUT')
        self.assertNotIn('private token',summary)
        self.assertIn('已停止本轮检查',summary)
        self.assertIsNone(self.queue.tick(self.checker()))

    def test_malformed_checker_receipt_does_not_leave_running_forever(self):
        self.submit()
        self.queue.tick(SimpleNamespace(run=lambda job:{'status':'unexpected'}))
        self.assertEqual(self.queue.latest(self.user)['state'],'NEEDS_INPUT')

    def test_preparation_thread_keeps_working_during_long_hardware_execution(self):
        self.submit()
        self.store.submit(self.user,self.session,'hardware',{'action':'replay'})
        running=threading.Event();prepared=threading.Event();errors=[]
        drain=self.root/'runtime/service/drain.json'
        def check(_self,job):
            if not running.wait(5):raise AssertionError('replay never started')
            prepared.set()
            return {'status':'STATIC_PASSED'}
        class BlockingExecutor:
            def __init__(self,*args):pass
            def execute(self,job):
                running.set()
                if not prepared.wait(5):raise AssertionError('material checks blocked behind replay')
                drain.parent.mkdir(parents=True,exist_ok=True);drain.write_text('{}')
                return {'simulated':True,'task':'SIMULATION/no-hardware'}
        def worker():
            try:run(self.cfg,self.store)
            except Exception as exc:errors.append(exc)
        with patch.dict(os.environ,{'BENCH_GATEWAY_MANAGED':'1'}),patch('bench_gateway.worker.Executor',BlockingExecutor),patch('bench_gateway.preparation.MaterialCheck.run',check):
            thread=threading.Thread(target=worker);thread.start();thread.join(10)
            if thread.is_alive():
                drain.parent.mkdir(parents=True,exist_ok=True);drain.write_text('{}');thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        self.assertTrue(prepared.is_set())
        self.assertEqual(self.queue.latest(self.user)['state'],'STATIC_PASSED')

    def test_scene_selection_accepts_clear_ranges_and_deduplicates(self):
        self.assertEqual(scene_selection('全部',18),list(range(1,19)))
        self.assertEqual(scene_selection('1,3,5-8,3',18),[1,3,5,6,7,8])
        for text in ('1-3','1 - 3','1 到 3','第1至3场景'):
            with self.subTest(text=text):self.assertEqual(scene_selection(text,18),[1,2,3])

    def test_scene_selection_rejects_ambiguous_or_out_of_range_requests(self):
        for text in ('','0','19','3-1','1-19','1.5','1;2','一到三','1--3'):
            with self.subTest(text=text):
                with self.assertRaises(GatewayError):scene_selection(text,18)
        for count in (0,-1,True,'18'):
            with self.subTest(count=count):
                with self.assertRaises(GatewayError):scene_selection('全部',count)

    def material_service(self,assets,dataset,validator_error=False):
        calls=[]
        def python(code,args=(),timeout=60,**kwargs):
            if 'print(json.dumps(check_source(' in code:
                calls.append(('dataset',timeout,json.loads(base64.b64decode(args[0]))))
                if validator_error:raise GatewayError('dataset manifest changed')
                return json.dumps(dataset['data'])
            calls.append(('inspector',timeout,json.loads(base64.b64decode(args[0]))))
            return json.dumps({'schema_version':1,'status':'PASS','checks':[],'issues':[]})
        ssh=SimpleNamespace(python=python)
        registry=SimpleNamespace(ssh=ssh,list=lambda user:assets)
        service=SimpleNamespace(registry=registry,datasets=SimpleNamespace(ssh=ssh),asset=lambda *args:dataset)
        return service,calls

    def material_config(self):
        return {**self.cfg,'project_root':str(Path(__file__).resolve().parents[1])}

    def test_registered_dependency_cannot_impersonate_camera_or_lidar_role(self):
        asset={'id':'a-m100','name':'M100-only','kind':'dependency','owner':self.user,'version':1,
               'data':{'remote_path':'/private/M100.so','sha256':'a'*64,'target':'APP/lib/liburoad_m100.so'}}
        dataset={'origin':'dataset_catalog','data':{'scene_count':18}}
        service,calls=self.material_service([asset],dataset)
        draft={'camera_so':'M100-only','lidar_so':'M100-only','m100_so':'M100-only','dataset':'共享18场景','scene_scope':'全部'}
        with patch('bench_gateway.service.Service',return_value=service):
            result=MaterialCheck(self.material_config(),self.store).run({'owner':self.user,'payload':draft})
        self.assertEqual(result['status'],'NEEDS_INPUT')
        rejected={item['field'] for item in result['issues'] if item['code']=='SOURCE_UNAVAILABLE'}
        self.assertEqual(rejected,{'camera_so','lidar_so'})
        request=next(call[2] for call in calls if call[0]=='inspector')
        self.assertIsNone(request['camera_so']);self.assertIsNone(request['lidar_so'])
        self.assertEqual(request['m100_so']['target'],'APP/lib/liburoad_m100.so')

    def test_private_dataset_is_revalidated_before_marking_source_passed(self):
        dataset={'data':{'remote_path':'/private/dataset','manifest':'/private/dataset/manifest.json',
                         'sha256':'b'*64,'scene_count':18}}
        draft={'dataset':'private dataset','scene_scope':'1-3'}
        for broken in (False,True):
            with self.subTest(changed_manifest=broken):
                service,calls=self.material_service([],dataset,validator_error=broken)
                with patch('bench_gateway.service.Service',return_value=service):
                    result=MaterialCheck(self.material_config(),self.store).run({'owner':self.user,'payload':draft})
                verified=[call for call in calls if call[0]=='dataset']
                self.assertEqual(len(verified),1)
                self.assertEqual(verified[0][2],dataset['data'])
                self.assertGreater(verified[0][1],0);self.assertLessEqual(verified[0][1],120)
                passed=[c for c in result['checks'] if c['field']=='dataset' and c['status']=='PASS']
                if broken:
                    self.assertEqual(result['status'],'NEEDS_INPUT');self.assertEqual(passed,[])
                    self.assertTrue(any(i['field']=='dataset' for i in result['issues']))
                else:
                    self.assertEqual(len(passed),1);self.assertEqual(result['scenes'],[1,2,3])


if __name__=='__main__':unittest.main()
