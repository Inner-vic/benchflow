import copy,io,json,os,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from bench_gateway import ai_broker,cloud_dataset_worker as worker
from bench_gateway.ephemeral_ai import run
from bench_gateway.core import GatewayError
import test_cloud_datasets as fixture

class WorkstationStorageTests(unittest.TestCase):
    def test_ai_client_lifecycle_creates_no_pc_business_state(self):
        from bench_gateway.config import load
        from bench_gateway.deployment import status
        from bench_gateway.windows_host import run_host
        from bench_gateway.login_startup import run as login
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config';p.mkdir();(p/'gateway.json').write_text('{"deployment_role":"ai-client"}')
            cfg=load(d);self.assertNotIn('state_root',cfg)
            with patch('bench_gateway.ai_client_admin.status',return_value={'active':True}):self.assertEqual({'active':True},status(cfg))
            with patch('bench_gateway.ephemeral_ai.serve') as serve:run_host(cfg);serve.assert_called_once_with(cfg)
            with patch('bench_gateway.login_startup.start',return_value={'state':'START_REQUESTED'}):self.assertEqual('START_REQUESTED',login(cfg)['state'])
            self.assertEqual(['config'],[p.name for p in Path(d).iterdir()])
    def test_offline_ai_broker_fails_fast_without_material_admission(self):
        with tempfile.TemporaryDirectory() as d:
            cfg={'state_root':d,'ai_backend':'workstation-broker'}
            with self.assertRaisesRegex(GatewayError,'未连接'):ai_broker.request(cfg,'yaml','private prompt',60)
            with ai_broker.connection(cfg) as c:self.assertEqual(0,c.execute('SELECT count(*) FROM requests').fetchone()[0])
            ai_broker.touch(cfg);self.assertTrue(ai_broker.health(cfg)['connected'])
    def test_ai_request_claim_and_result_are_persisted_only_in_workstation_database(self):
        with tempfile.TemporaryDirectory() as d:
            cfg={'state_root':d};answer=[]
            t=threading.Thread(target=lambda:answer.append(ai_broker.request(cfg,'yaml','one request',5)))
            t.start();job=None
            for _ in range(100):
                job=ai_broker.claim(cfg)
                if job:break
                time.sleep(.02)
            self.assertIsNotNone(job);self.assertIsNone(ai_broker.claim(cfg))
            ai_broker.finish(cfg,{'id':job['id'],'claim':'wrong','result':{'x':0}})
            ai_broker.finish(cfg,{'id':job['id'],'claim':job['claim'],'result':{'x':1}})
            t.join(2);self.assertEqual([{'x':1}],answer)
            with ai_broker.connection(cfg) as c:self.assertEqual('DONE',c.execute('SELECT state FROM requests').fetchone()[0])
    def test_pc_parser_reads_stdout_and_never_requests_output_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config';p.mkdir();(p/'yaml-schema.json').write_text('{}')
            outcome=type('P',(),{'returncode':0,'stdout':json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{"status":"READY"}'}})})()
            with patch('bench_gateway.parser.command',return_value=['codex','exec','--ephemeral','--output-last-message','unused','-']),patch('bench_gateway.ephemeral_ai.subprocess.run',return_value=outcome) as process:
                self.assertEqual({'status':'READY'},run({'project_root':d},'yaml','private prompt',3))
            self.assertNotIn('--output-last-message',process.call_args.args[0]);self.assertEqual('private prompt',process.call_args.kwargs['input'])
            self.assertEqual(['config'],[p.name for p in Path(d).iterdir()])
    def test_unexpected_pc_parser_tool_event_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config';p.mkdir();(p/'yaml-schema.json').write_text('{}')
            outcome=type('P',(),{'returncode':0,'stdout':json.dumps({'type':'item.completed','item':{'type':'command_execution'}})})()
            with patch('bench_gateway.parser.command',return_value=['codex','--output-last-message','unused']),patch('bench_gateway.ephemeral_ai.subprocess.run',return_value=outcome):
                with self.assertRaises(GatewayError):run({'project_root':d},'yaml','prompt',3)
    def test_shared_dataset_uses_original_name_and_reuses_scenes_across_users_and_scopes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();client=fixture.S3();first=worker.discover(client,fixture.LOCATION,fixture.SOURCE,[1])
            all_scenes=worker.discover(client,fixture.LOCATION,fixture.SOURCE,[1,2])
            def manifest(directory,plan):
                f=directory/'manifest.json';f.write_text('{}')
                return {'manifest':str(f),'sha256':worker.hashlib.sha256(f.read_bytes()).hexdigest(),'cloud_source':{}}
            with patch.object(worker,'prepare_manifest',side_effect=manifest),patch.object(worker.shutil,'disk_usage') as space:
                space.return_value.free=100*1024**3
                one=worker.download(client,fixture.LOCATION,fixture.SOURCE,first,root/'alice',shared_root=root/'datasets')
                self.assertEqual(8,len(client.reads));self.assertTrue((root/'datasets/custom/source.json').is_file())
                again=worker.download(client,fixture.LOCATION,fixture.SOURCE,first,root/'bob',shared_root=root/'datasets')
                self.assertTrue(again['reused']);self.assertEqual(8,len(client.reads))
                two=worker.download(client,fixture.LOCATION,fixture.SOURCE,all_scenes,root/'bob',shared_root=root/'datasets')
                self.assertEqual(16,len(client.reads))
                relative=first['files'][0]['relative'];one_file=Path(one['data']['manifest']).parent/relative;two_file=Path(two['data']['manifest']).parent/relative
                self.assertTrue(os.path.samestat(one_file.stat(),two_file.stat()))
                self.assertFalse((root/'alice').exists());self.assertFalse((root/'bob').exists())
    def test_same_dataset_name_from_different_source_is_never_silently_merged(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();base=root/'datasets/custom';base.mkdir(parents=True)
            (base/'source.json').write_text(json.dumps({'volume':'different','relative_root':'custom'}))
            client=fixture.S3();plan=worker.discover(client,fixture.LOCATION,fixture.SOURCE,[1])
            with self.assertRaisesRegex(ValueError,'another source'):
                worker.download(client,fixture.LOCATION,fixture.SOURCE,plan,root/'alice',shared_root=root/'datasets')
            self.assertEqual([],client.reads)

if __name__=='__main__':unittest.main()
