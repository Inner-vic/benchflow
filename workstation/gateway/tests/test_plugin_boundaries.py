import io,json,sqlite3,tempfile,unittest
from contextlib import contextmanager,redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from bench_gateway import roadmodel_plugin as plugin,plugin_routing,roadmodel_shared as shared

class PluginBoundaries(unittest.TestCase):
    def test_pcm_text_never_loads_other_backend(self):
        with patch.object(plugin,'backend',side_effect=AssertionError('should not load')):
            self.assertIsNone(plugin.handle(SimpleNamespace(cfg={}), 'feishu:oc_a:ou_a','当前配置'))
    def test_pcm_callback_is_classified_without_backend(self):
        from bench_gateway.card_ui import PREFIX
        with patch('bench_gateway.card_ui.decode',return_value={'action':'confirm_materials'}):
            self.assertFalse(plugin_routing.roadmodel({},'s',PREFIX+'test'))
    def test_roadmodel_failure_is_explicit_and_not_pcm_fallback(self):
        with patch.object(plugin,'backend',side_effect=ImportError('dependency unavailable')),redirect_stderr(io.StringIO()):
            result=plugin.handle(SimpleNamespace(cfg={}), 'feishu:oc_a:ou_a','RoadModel评测')
        self.assertIn('暂时不可用',result);self.assertIn('未转交',result)
    def test_backend_recovery_failure_does_not_skip_next_backend(self):
        good=SimpleNamespace(runtime=Mock(return_value='rt'))
        with patch.object(plugin,'backend',side_effect=[ImportError('offline'),SimpleNamespace(recover=Mock()),good]),patch('bench_gateway.roadmodel_shared.recover') as recover,redirect_stderr(io.StringIO()):
            plugin.recover({})
        recover.assert_called_once_with({},'rt')
    def test_current_external_entry_is_preserved(self):
        entry=SimpleNamespace(COMMANDS={'RoadModel评测'},handle=Mock(return_value='NO_REPLY'))
        with patch.object(plugin,'backend',return_value=entry):
            self.assertEqual('NO_REPLY',plugin.handle(SimpleNamespace(cfg={}), 's','RoadModel评测'))
        entry.handle.assert_called_once()
    def test_non_fifo_board_paths_stay_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'configs').mkdir()
            for name in ('evaluation.json','delivery_demo.json'):
                (root/'configs'/name).write_text(json.dumps({'bench_available':True}))
            backends={n:SimpleNamespace(tick=Mock(),runtime=Mock(return_value='rt')) for n in ('evaluation_entry','delivery_demo_entry','model_ticket_entry','feishu_entry')}
            with patch.object(plugin,'ROOT',root),patch.object(plugin,'backend',side_effect=backends.get),patch('bench_gateway.roadmodel_shared.tick') as shared_tick,redirect_stderr(io.StringIO()):
                plugin.tick({})
            backends['evaluation_entry'].tick.assert_not_called()
            backends['delivery_demo_entry'].tick.assert_not_called()
            backends['model_ticket_entry'].tick.assert_called_once()
            shared_tick.assert_called_once()

class SharedBoundary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite3'
        @contextmanager
        def connect():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
            try:
                with c:yield c
            finally:c.close()
        self.connect=connect
        with connect() as c:
            c.executescript('create table tasks(id text,owner text,state text,updated real,notice text);create table chains(task text,kind text,state text,detail text);')
            c.execute('insert into tasks values(?,?,?,?,?)',('rm-unit','ou_test','RUNNING',0,''))
        self.rt=SimpleNamespace(connect=connect,get=lambda tid,user:{'id':tid,'owner':user,'state':'RUNNING'})
    def chain(self,mode=shared.MODE):
        old={'state':'RUNNING','mode':mode,'owner':'ou_test','task':'rm-unit','hardware_job':'gw-fixed','binding':{'id':'frozen'}}
        with self.connect() as c:c.execute('insert into chains values(?,?,?,?)',('rm-unit','compile_alignment','RUNNING',json.dumps(old)))
        return old
    def read(self):
        with self.connect() as c:return dict(c.execute('select * from chains').fetchone())
    def test_busy_keeps_same_hardware_identity_and_running_business(self):
        old=self.chain();queue=SimpleNamespace(collect=Mock(return_value={**old,'state':'QUEUED'}))
        shared.refresh({},self.rt,queue);r=self.read()
        self.assertEqual('RUNNING',r['state']);self.assertEqual('gw-fixed',json.loads(r['detail'])['hardware_job'])
    def test_recovery_preserves_valid_queued_board_unit(self):
        old=self.chain();queue=SimpleNamespace(collect=Mock(return_value={**old,'state':'QUEUED'}))
        shared.recover({},self.rt,queue);self.assertEqual('RUNNING',self.read()['state'])
    def test_unknown_started_board_is_not_restarted(self):
        old=self.chain();queue=SimpleNamespace(collect=Mock(return_value={**old,'state':'RUNNING'}))
        shared.recover({},self.rt,queue);self.assertEqual('NEEDS_REVIEW',self.read()['state'])
    def test_bad_owner_or_job_proof_becomes_review(self):
        self.chain();queue=SimpleNamespace(collect=Mock(side_effect=ValueError('wrong identity')))
        shared.refresh({},self.rt,queue);self.assertEqual('NEEDS_REVIEW',self.read()['state'])
    def test_completed_board_transfers_to_read_only_native_collection(self):
        self.chain();detail={'state':'RUNNING','mode':'active_native_execution','binding':{'id':'frozen'},'native_finished':True}
        shared.refresh({},self.rt,SimpleNamespace(collect=Mock(return_value=detail)))
        self.assertEqual(detail,json.loads(self.read()['detail']))
    def test_only_new_compile_unit_enters_fifo(self):
        queue=SimpleNamespace(submit=Mock(return_value={'state':'QUEUED','mode':shared.MODE,'hardware_job':'gw-fixed'}),collect=Mock())
        native=Mock(return_value={'state':'SUCCEEDED'})
        def tick(executor):
            result=executor({'kind':'compile_alignment'},Path('/state/runs/rm-unit/compile_alignment'))
            executor({'kind':'model_accuracy'},Path('/state/runs/rm-unit/model_accuracy'))
            executor({'kind':'compile_alignment','retained_result':True},Path('/state/runs/rm-unit/compile_alignment'))
            return result
        self.rt.tick=tick
        r=shared.tick({},self.rt,queue,native)
        self.assertEqual('RUNNING',r['state']);queue.submit.assert_called_once();self.assertEqual(2,native.call_count)

if __name__=='__main__':unittest.main()
