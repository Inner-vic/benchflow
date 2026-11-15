import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from bench_gateway.maintenance import notice,MESSAGE
from bench_gateway import cc_agent

class MaintenanceTests(unittest.TestCase):
    def test_hot_read_and_invalid_file_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg={'project_root':d};p=Path(d)/'runtime/maintenance.json';p.parent.mkdir()
            self.assertEqual('',notice(cfg));p.write_text('{"enabled":true}')
            self.assertEqual(MESSAGE,notice(cfg));p.write_text('{"enabled":false}')
            self.assertEqual('',notice(cfg));p.write_text('bad');self.assertEqual(MESSAGE,notice(cfg))
    def test_all_inbound_types_return_notice_before_parsing_or_material_mutations(self):
        service=Mock();cfg={'project_root':'unused','cc_project':'bench'}
        for message in ('你好','@@BENCH_CARD@@signed-packet','@@BENCH_MENU@@packet','附件内容'):
            output=io.StringIO()
            with self.subTest(message=message),patch.object(cc_agent,'load',return_value=cfg),patch.object(cc_agent,'Service',return_value=service),patch('bench_gateway.maintenance.notice',return_value=MESSAGE),patch.object(cc_agent.sys,'stdin',io.StringIO(message)),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':'feishu:oc_test:ou_test'}),contextlib.redirect_stdout(output):
                cc_agent.main()
            lines=[json.loads(x) for x in output.getvalue().splitlines()]
            self.assertEqual(MESSAGE,lines[1]['item']['text']);self.assertEqual('turn.completed',lines[-1]['type'])
        service.update_inputs.assert_not_called();service.handle.assert_not_called()
