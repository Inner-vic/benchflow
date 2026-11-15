"""Authentication boundaries use dummy credentials and synthetic HTTP responses."""
import ast
import base64
import contextlib
import gzip
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from bench_gateway.asset_fetch import REMOTE_FETCH, REMOTE_HTTP, REMOTE_PROBE, fetch, probe_download
from bench_gateway.core import GatewayError, Store


class Response(io.BytesIO):
    def __init__(self, body, status=206):
        super().__init__(body)
        self.headers={'Content-Length':str(len(body)),'Content-Type':'application/octet-stream'}
        self.status=status

    def getcode(self):return self.status


class AuthenticatedFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.url='https://downloads.example.invalid/A.app.tar.gz'
        self.header='Basic '+base64.b64encode(b'alice:DO-NOT-LEAK-DUMMY').decode()
        self.request={'url':self.url,'hosts':['downloads.example.invalid','other.example.invalid'],
                      'root':str(self.root/'owner'),'kind':'package','timeout':20,'maximum':4096,
                      'authorization_host':'downloads.example.invalid','authorization':self.header}

    def execute(self, code, *, body=None, status=206, error=None, request=None):
        seen=[];body=gzip.compress(b'fixture') if body is None else body
        def opener(handler):
            def open_url(req,timeout):
                seen.append(req)
                if error:raise error
                return Response(body,status)
            return SimpleNamespace(open=open_url)
        output=io.StringIO();encoded=base64.b64encode(json.dumps(request or self.request).encode()).decode()
        with patch('sys.argv',['fetch',encoded]),patch('urllib.request.build_opener',side_effect=opener),contextlib.redirect_stdout(output):
            try:exec(compile(code,'<download-test>','exec'),{})
            except SystemExit:pass
        return json.loads(output.getvalue()),seen

    def test_authorized_download_sends_header_but_receipt_and_files_do_not_contain_it(self):
        result,seen=self.execute(REMOTE_FETCH)
        self.assertEqual('PASS',result['state'])
        self.assertEqual(self.header,seen[0].get_header('Authorization'))
        self.assertNotIn(self.header,json.dumps(result))
        for path in self.root.rglob('*'):
            if path.is_file():self.assertNotIn(b'DO-NOT-LEAK',path.read_bytes())

    def test_credentials_cannot_be_applied_to_a_different_original_host(self):
        result,seen=self.execute(REMOTE_FETCH,request={**self.request,'authorization_host':'other.example.invalid'})
        self.assertEqual('INVALID_AUTH_CONFIGURATION',result['code']);self.assertEqual([],seen)

    def test_redirects_keep_auth_only_on_same_https_origin_and_never_restore_after_cross_origin(self):
        namespace={'r':self.request,'urllib':urllib,'re':__import__('re')}
        def check(url):
            parsed=urllib.parse.urlsplit(url)
            if parsed.scheme!='https' or parsed.hostname not in self.request['hosts']:raise ValueError()
            return parsed
        namespace['check']=check
        exec(REMOTE_HTTP,namespace)
        redirect=namespace['Redirect']()
        initial=namespace['authenticated_request'](self.url)
        same=redirect.redirect_request(initial,None,302,'Found',{},'https://downloads.example.invalid/new')
        self.assertEqual(self.header,same.get_header('Authorization'))
        foreign=redirect.redirect_request(initial,None,302,'Found',{},'https://other.example.invalid/new')
        self.assertIsNone(foreign.get_header('Authorization'))
        back=redirect.redirect_request(foreign,None,302,'Found',{},self.url)
        self.assertIsNone(back.get_header('Authorization'))

    def test_probe_checks_get_magic_with_512_byte_read_budget_and_no_asset_files(self):
        result,seen=self.execute(REMOTE_PROBE,body=b'\x1f\x8b\x08'+b'A'*1000)
        self.assertEqual('PASS',result['state']);self.assertEqual(512,result['sample_bytes_read'])
        self.assertEqual('bytes=0-511',seen[0].get_header('Range'))
        self.assertEqual(self.header,seen[0].get_header('Authorization'))
        self.assertEqual([],list(self.root.iterdir()))

    def test_probe_auth_failure_is_single_attempt_and_sanitizes_server_error(self):
        error=urllib.error.HTTPError(self.url,401,'DO-NOT-LEAK',{},None)
        result,seen=self.execute(REMOTE_PROBE,error=error)
        self.assertEqual({'state':'AUTH_REQUIRED','http_status':401},result)
        self.assertEqual(1,len(seen));self.assertEqual([],list(self.root.iterdir()))

    def test_probe_does_not_report_success_for_html_or_empty_response(self):
        for body in (b'<html>login</html>',b'',b'not an archive'):
            with self.subTest(body=body):
                result,_=self.execute(REMOTE_PROBE,body=body)
                self.assertEqual('UNEXPECTED_ARTIFACT',result['state'])

    def test_gateway_credentials_are_confined_to_ssh_stdin_and_never_registered_as_metadata(self):
        store=Mock();store.remote_root.return_value=str(self.root/'owner')
        registry=SimpleNamespace(store=store,cfg={'download_hosts':['downloads.example.invalid'],
            'remote_users_root':'/fixture/users'},ssh=Mock(),record=Mock())
        data={'remote_path':'/fixture/users/alice/package/hash/asset.bin','sha256':'a'*64,'size':10,'filename':'A.app.tar.gz'}
        registry.ssh.python.return_value=json.dumps({'state':'PASS','data':data})
        with patch('bench_gateway.download_auth.load_authorization',return_value=self.header) as auth:
            fetch(registry,'ou_alice','package','A',self.url)
        auth.assert_called_once_with(registry.cfg,'ou_alice','downloads.example.invalid')
        code=registry.ssh.python.call_args.args[0]
        encoded=ast.literal_eval(ast.parse(code).body[1].value)[1]
        payload=json.loads(base64.b64decode(encoded))
        self.assertEqual(self.header,payload['authorization'])
        self.assertEqual(1,len(registry.ssh.python.call_args.args))
        self.assertNotIn(self.header,repr(registry.record.call_args))

    def test_probe_receipt_allowlist_drops_unexpected_server_content(self):
        cfg={'project_root':str(self.root),'state_root':str(self.root/'state'),
             'download_hosts':['downloads.example.invalid']}
        Store(cfg['state_root']).allow('ou_alice')
        fake=Mock();fake.python.return_value=json.dumps({'state':'PASS','http_status':206,
            'sample_bytes_read':512,'gzip_magic':True,'secret':'DO-NOT-LEAK'})
        with patch('bench_gateway.download_auth.load_authorization',return_value=self.header),patch('bench_gateway.transfer.SSH',return_value=fake):
            result=probe_download(cfg,'ou_alice',self.url)
        self.assertNotIn('DO-NOT-LEAK',json.dumps(result));self.assertEqual(0,result['asset_writes'])
        self.assertTrue(result['credentials_configured'])
        self.assertEqual(1,len(fake.python.call_args.args))


if __name__=='__main__':unittest.main()
