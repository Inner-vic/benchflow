import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bench_gateway.cloud_browser_start import request_browser_start
from bench_gateway.cloud_bootstrap import TARGET


class BrowserStartTests(unittest.TestCase):
    def test_actual_tool_evidence_required_and_denial_is_not_retried(self):
        profile=dict(zip(('instance','namespace','cluster'),TARGET))
        samples=[('START_REQUESTED',True,'completed',0,'STARTING'),
                 ('ALREADY_RUNNING',False,'completed',0,'STARTING'),
                 ('START_REQUESTED',False,'completed',0,'START_RESULT_UNKNOWN'),
                 ('START_REQUESTED',True,'failed',0,'START_RESULT_UNKNOWN'),
                 ('ACCESS_DENIED',False,'failed',0,'START_REJECTED'),
                 ('LOGIN_REQUIRED',False,'completed',0,'START_REJECTED'),
                 ('UNKNOWN',False,'completed',0,'START_RESULT_UNKNOWN'),
                 ('START_REQUESTED',True,'completed',1,'START_RESULT_UNKNOWN')]
        for outcome,clicked,tool_status,code,expected in samples:
            with self.subTest(outcome=outcome,tool_status=tool_status,code=code),TemporaryDirectory() as root:
                def launch(argv,**kwargs):
                    directory=Path(argv[argv.index('--cd')+1])
                    class Child:
                        returncode=code
                        def communicate(self,prompt,timeout):
                            self.prompt=prompt
                            reply={'outcome':outcome,'clicked_start':clicked,'instance_name':'bench-eval-offline','visible_state':'starting','problem':''}
                            (directory/'result.json').write_text(json.dumps(reply))
                            kwargs['stdout'].write((json.dumps({'item':{'type':'mcp_tool_call','server':'cua_repl','tool':'js','status':tool_status}})+'\n').encode())
                            kwargs['stdout'].flush()
                    return Child()
                with patch('bench_gateway.cloud_browser_start.resolve_binary',return_value='fixed-codex'),patch('bench_gateway.cloud_browser_start.subprocess.Popen',side_effect=launch) as popen:
                    result=request_browser_start({},profile,root)
                self.assertEqual(result['state'],expected)
                self.assertEqual(popen.call_count,1)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',popen.call_args.args[0])

    def test_other_instance_is_rejected_before_launch(self):
        with patch('bench_gateway.cloud_browser_start.subprocess.Popen') as popen:
            self.assertEqual(request_browser_start({}, {'instance':'other'}, 'unused')['state'],'START_REJECTED')
            popen.assert_not_called()


if __name__=='__main__':unittest.main()
