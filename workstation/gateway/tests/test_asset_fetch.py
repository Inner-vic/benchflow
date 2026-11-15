import ast
import base64
import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from bench_gateway.asset_fetch import REMOTE_FETCH, fetch
from bench_gateway.core import GatewayError
from bench_gateway.transfer import SSH
from tools.manage_download_hosts import DownloadHostError, main as hosts_main, manage as manage_hosts


class Response(io.BytesIO):
    def __init__(self,body,headers):
        super().__init__(body)
        self.headers=headers


class AssetFetchTests(unittest.TestCase):
    def setUp(self):
        authorization=patch('bench_gateway.download_auth.load_authorization',return_value=None)
        authorization.start();self.addCleanup(authorization.stop)
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name).resolve()
        self.root=self.base/'users'/'alice'
        self.payload=gzip.compress(b'package-bytes-for-download-test',mtime=0)
        self.request={'root':str(self.root),'kind':'package','url':'https://downloads.example.invalid/A.app.tar.gz',
                      'hosts':['downloads.example.invalid'],'maximum':1024,'timeout':30}

    def tearDown(self):self.temp.cleanup()

    def run_remote(self,*,body=None,headers=None,redirect=None,error=None,request=None):
        request=request or self.request
        body=self.payload if body is None else body
        headers={'Content-Length':str(len(body))} if headers is None else headers
        outgoing=[]

        def opener(handler):
            def open_url(url,timeout):
                outgoing.append(url)
                if redirect:
                    handler.redirect_request(urllib.request.Request(url),None,302,'Found',{},redirect)
                    outgoing.append(redirect)
                if error:raise error
                return Response(body,headers)
            return SimpleNamespace(open=open_url)

        encoded=base64.b64encode(json.dumps(request).encode()).decode()
        output=io.StringIO()
        with patch('sys.argv',['fetch',encoded]),patch('urllib.request.build_opener',side_effect=opener),contextlib.redirect_stdout(output):
            try:exec(compile(REMOTE_FETCH,'<remote-fetch>','exec'),{})
            except SystemExit as exc:self.assertEqual(exc.code,0)
        return json.loads(output.getvalue()),outgoing

    def assert_no_download_residue(self):
        if self.root.exists():self.assertEqual(list(self.root.rglob('.download-*')),[])

    def test_direct_workstation_download_uses_hash_path_and_reuses_same_content(self):
        result,outgoing=self.run_remote()
        expected_hash=hashlib.sha256(self.payload).hexdigest()
        final=self.root/'package'/expected_hash/'asset.bin'
        self.assertEqual(result['state'],'PASS')
        self.assertEqual(Path(result['data']['remote_path']),final.resolve())
        self.assertEqual(result['data']['sha256'],expected_hash)
        self.assertEqual(result['data']['size'],len(self.payload))
        self.assertEqual(result['data']['filename'],'A.app.tar.gz')
        self.assertEqual(final.read_bytes(),self.payload)
        self.assertEqual(outgoing,[self.request['url']])
        self.assertEqual(self.run_remote()[0]['state'],'PASS')
        self.assertEqual(list((self.root/'package').rglob('asset.bin')),[final])
        self.assert_no_download_residue()

    def test_foreign_domain_unsafe_scheme_credentials_and_port_never_connect(self):
        for url in ('https://foreign.invalid/a','http://downloads.example.invalid/a',
                    'https://name:secret@downloads.example.invalid/a','https://downloads.example.invalid:444/a'):
            with self.subTest(url=url):
                result,outgoing=self.run_remote(request={**self.request,'url':url})
                self.assertEqual(result,{'state':'FAILED','code':'URL_NOT_ALLOWED'})
                self.assertEqual(outgoing,[])

    def test_foreign_domain_and_http_redirects_are_rejected_before_following(self):
        for redirect in ('https://foreign.invalid/secret','http://downloads.example.invalid/secret'):
            with self.subTest(redirect=redirect):
                result,outgoing=self.run_remote(redirect=redirect)
                self.assertEqual(result,{'state':'FAILED','code':'URL_NOT_ALLOWED'})
                self.assertEqual(outgoing,[self.request['url']])
                self.assert_no_download_residue()

    def test_allowed_https_redirect_retains_workstation_destination(self):
        redirected='https://downloads.example.invalid/relocated/A.app.tar.gz'
        result,outgoing=self.run_remote(redirect=redirected)
        self.assertEqual(result['state'],'PASS')
        self.assertEqual(outgoing,[self.request['url'],redirected])
        self.assertIn(self.root,Path(result['data']['remote_path']).parents)

    def test_authentication_errors_do_not_return_token_url_or_server_message(self):
        secret_url=self.request['url']+'?token=SECRET-DOWNLOAD-TOKEN'
        for code in (401,403):
            with self.subTest(code=code):
                error=urllib.error.HTTPError(secret_url,code,'SECRET-DOWNLOAD-TOKEN in response',{},None)
                result,_=self.run_remote(error=error,request={**self.request,'url':secret_url})
                self.assertEqual(result,{'state':'FAILED','code':'AUTH_REQUIRED'})
                self.assertNotIn('SECRET',json.dumps(result))
                self.assert_no_download_residue()

    def test_declared_size_and_streamed_size_enforce_limit(self):
        for headers in ({'Content-Length':'99999'},{}):
            with self.subTest(headers=headers):
                result,_=self.run_remote(body=b'x'*65,headers=headers,request={**self.request,'maximum':64})
                self.assertEqual(result,{'state':'FAILED','code':'TOO_LARGE'})
                self.assert_no_download_residue()
        self.assertEqual(list(self.root.rglob('asset.bin')),[])

    def test_empty_and_truncated_bodies_are_not_registered(self):
        for body,length in ((b'',0),(self.payload[:10],100)):
            with self.subTest(length=length):
                result,_=self.run_remote(body=body,headers={'Content-Length':str(length)})
                self.assertEqual(result,{'state':'FAILED','code':'INCOMPLETE'})
                self.assert_no_download_residue()
        self.assertEqual(list(self.root.rglob('asset.bin')),[])

    def test_html_json_and_login_mime_are_rejected_before_body_read(self):
        for mime in ('text/html; charset=utf-8','application/xhtml+xml','application/json',
                     'application/problem+json','TEXT/HTML'):
            with self.subTest(mime=mime),patch.object(Response,'read',side_effect=AssertionError('body must not be read')):
                result,_=self.run_remote(headers={'Content-Type':mime})
                self.assertEqual(result,{'state':'FAILED','code':'RESPONSE_NOT_ARTIFACT'})
                self.assert_no_download_residue()
        self.assertEqual(list(self.root.rglob('asset.bin')),[])

    def test_login_page_and_json_disguised_as_octet_stream_are_not_assets(self):
        for body in (b' \r\n<!DOCTYPE html><form action="/login">SECRET</form>',
                     b'\xef\xbb\xbf  <html>SECRET</html>',b'{"error":"SECRET"}',b' ["SECRET"]'):
            with self.subTest(body=body):
                result,_=self.run_remote(body=body,headers={'Content-Type':'application/octet-stream'})
                self.assertEqual(result,{'state':'FAILED','code':'RESPONSE_NOT_ARTIFACT'})
                self.assertNotIn('SECRET',json.dumps(result))
                self.assert_no_download_residue()
        self.assertEqual(list(self.root.rglob('asset.bin')),[])

    def test_same_host_sso_redirect_returning_login_html_is_not_registered(self):
        result,_=self.run_remote(body=b'<html>sign in</html>',redirect='https://downloads.example.invalid/login',
                                 headers={'Content-Type':'text/html'})
        self.assertEqual(result,{'state':'FAILED','code':'RESPONSE_NOT_ARTIFACT'})
        self.assertEqual(list(self.root.rglob('asset.bin')),[])
        self.assert_no_download_residue()

    def test_package_requires_gzip_magic_and_so_target_requires_elf_magic(self):
        result,_=self.run_remote(body=b'plain archive or server error')
        self.assertEqual(result,{'state':'FAILED','code':'UNEXPECTED_FORMAT'})
        request={**self.request,'kind':'dependency','target':'lib/libxcamera.so'}
        for target in ('lib/libxcamera.so','lib/libxcamera.so.1.2'):
            result,_=self.run_remote(body=self.payload,request={**request,'target':target})
            self.assertEqual(result,{'state':'FAILED','code':'UNEXPECTED_FORMAT'})
        result,_=self.run_remote(body=b'\x7fELF'+b'\0'*60,request=request)
        self.assertEqual(result['state'],'PASS')
        # A download signature is not a static architecture or ABI claim.
        self.assertNotIn('elf',result['data'])
        self.assertNotIn('compatible',result['data'])
        self.assert_no_download_residue()

    def test_existing_hash_path_with_different_content_is_preserved_and_rejected(self):
        folder=self.root/'package'/hashlib.sha256(self.payload).hexdigest()
        folder.mkdir(parents=True)
        (folder/'asset.bin').write_bytes(b'existing unexpected content')
        result,_=self.run_remote()
        self.assertEqual(result,{'state':'FAILED','code':'EXISTING_CONTENT_CHANGED'})
        self.assertEqual((folder/'asset.bin').read_bytes(),b'existing unexpected content')
        self.assert_no_download_residue()

    def test_root_inbox_kind_hash_folder_and_final_symlinks_are_rejected(self):
        folder=self.root/'package'/hashlib.sha256(self.payload).hexdigest()
        for target in (self.root,self.root/'inbox',self.root/'package',folder,folder/'asset.bin'):
            with self.subTest(target=str(target)):
                real_is_symlink=Path.is_symlink
                with patch.object(Path,'is_symlink',lambda path: path==target or real_is_symlink(path)):
                    result,_=self.run_remote()
                self.assertEqual(result,{'state':'FAILED','code':'INVALID_ROOT'})
                self.assert_no_download_residue()

    def test_symlink_ancestor_of_user_root_is_rejected_before_download(self):
        # Model an existing ancestor symlink: root itself is not a symlink.
        # This exercises the same escape that resolve(root) would otherwise allow.
        self.root.parent.mkdir(parents=True)
        real_is_symlink=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda path: path==self.root.parent or real_is_symlink(path)):
            result,outgoing=self.run_remote()
        self.assertEqual(result,{'state':'FAILED','code':'INVALID_ROOT'})
        self.assertEqual(outgoing,[])

    def registry(self):
        store=Mock()
        store.remote_root.return_value=str(self.root)
        ssh=Mock()
        return SimpleNamespace(store=store,ssh=ssh,record=Mock(side_effect=lambda user,kind,name,data:data),
                               cfg={'download_hosts':['downloads.example.invalid'],'remote_users_root':str(self.base/'users')})

    def test_gateway_download_uses_only_workstation_ssh_and_preserves_owner(self):
        registry=self.registry()
        receipt={'remote_path':str(self.root/'dependency'/'hash'/'asset.bin'),'sha256':'a'*64,'size':123,'filename':'libxcamera.so'}
        registry.ssh.python.return_value=json.dumps({'state':'PASS','data':receipt})
        result=fetch(registry,'ou_alice','dependency','camera-A',self.request['url'],target='lib/libxcamera.so')
        registry.store.require_user.assert_called_once_with('ou_alice')
        registry.record.assert_called_once_with('ou_alice','dependency','camera-A',{**receipt,'target':'lib/libxcamera.so'})
        call=registry.ssh.python.call_args
        self.assertEqual(len(call.args),1)
        self.assertTrue(call.args[0].endswith(REMOTE_FETCH))
        assignment=ast.parse(call.args[0]).body[1]
        serialized=ast.literal_eval(assignment.value)[1]
        request=json.loads(base64.b64decode(serialized))
        self.assertEqual(request['root'],str(self.root))
        self.assertEqual(request['target'],'lib/libxcamera.so')
        self.assertEqual(result['target'],'lib/libxcamera.so')

    def test_signed_download_url_is_carried_only_in_ssh_stdin(self):
        registry=self.registry()
        registry.ssh=SSH({'workstation':'fixture-workstation'})
        receipt={'remote_path':str(self.root/'package'/'hash'/'asset.bin'),'sha256':'a'*64,'size':123,'filename':'A.app.tar.gz'}
        secret_url=self.request['url']+'?token=DO-NOT-ECHO'
        with patch('bench_gateway.transfer.subprocess.run',return_value=SimpleNamespace(
                returncode=0,stdout=json.dumps({'state':'PASS','data':receipt}),stderr='')) as process:
            result=fetch(registry,'ou_alice','package','A',secret_url)
        call=process.call_args
        self.assertEqual(call.args[0][-1],'python3 - ')
        self.assertNotIn('DO-NOT-ECHO',repr(call.args[0]))
        assignment=ast.parse(call.kwargs['input']).body[1]
        serialized=ast.literal_eval(assignment.value)[1]
        self.assertNotIn(serialized,repr(call.args[0]))
        self.assertEqual(json.loads(base64.b64decode(serialized))['url'],secret_url)
        self.assertNotIn('DO-NOT-ECHO',json.dumps(result))

    def test_gateway_does_not_register_http200_pages_or_wrong_artifact_types(self):
        registry=self.registry()
        for code in ('RESPONSE_NOT_ARTIFACT','UNEXPECTED_FORMAT'):
            with self.subTest(code=code):
                registry.ssh.python.return_value=json.dumps({'state':'FAILED','code':code})
                with self.assertRaises(GatewayError) as error:
                    fetch(registry,'ou_alice','package','A',self.request['url']+'?token=DO-NOT-ECHO')
                self.assertIn('本人工作站目录',str(error.exception))
                self.assertNotIn('DO-NOT-ECHO',str(error.exception))
        registry.record.assert_not_called()

    def test_gateway_rejects_dataset_and_unknown_host_without_ssh(self):
        registry=self.registry()
        for kind,url in (('dataset',self.request['url']),('package','https://foreign.invalid/asset')):
            with self.subTest(kind=kind,url=url),self.assertRaises(GatewayError):
                fetch(registry,'ou_alice',kind,'A',url)
        registry.ssh.python.assert_not_called();registry.record.assert_not_called()

    def test_gateway_malformed_url_is_a_sanitized_input_error_without_ssh(self):
        registry=self.registry()
        for url in ('https://downloads.example.invalid:PRIVATE-TOKEN/a','https://[PRIVATE-TOKEN/a'):
            with self.subTest(url=url):
                with self.assertRaises(GatewayError) as error:
                    fetch(registry,'ou_alice','package','A',url)
                self.assertNotIn('PRIVATE-TOKEN',str(error.exception))
        registry.ssh.python.assert_not_called();registry.record.assert_not_called()

    def test_gateway_auth_error_and_transport_exception_remain_sanitized(self):
        registry=self.registry()
        registry.ssh.python.return_value=json.dumps({'state':'FAILED','code':'AUTH_REQUIRED'})
        with self.assertRaises(GatewayError) as error:
            fetch(registry,'ou_alice','package','A',self.request['url']+'?token=DO-NOT-ECHO')
        self.assertIn('登录',str(error.exception));self.assertNotIn('DO-NOT-ECHO',str(error.exception))
        registry.ssh.python.side_effect=RuntimeError('DO-NOT-ECHO '+self.request['url'])
        with self.assertRaises(GatewayError) as error:
            fetch(registry,'ou_alice','package','A',self.request['url'])
        self.assertNotIn('DO-NOT-ECHO',str(error.exception));registry.record.assert_not_called()


class DownloadHostManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'config').mkdir()
        self.path=self.root/'config'/'gateway.json'
        self.data={'download_hosts':['existing.example.invalid'],'workstation':'unchanged-workstation',
                   'nested':{'retain':['all','fields']},'opaque_config_value':'DO-NOT-PRINT'}
        self.path.write_text(json.dumps(self.data),encoding='utf-8')

    def test_check_is_readonly_and_reports_only_host_policy_without_signed_url(self):
        before=self.path.read_bytes()
        with patch('urllib.request.urlopen',side_effect=AssertionError('no network')):
            result=manage_hosts('check',self.root,url='https://existing.example.invalid/a?token=SECRET')
        self.assertTrue(result['allowed'])
        self.assertEqual('NOT_RUN',result['network_actions'])
        self.assertEqual(before,self.path.read_bytes())
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertNotIn('DO-NOT-PRINT',json.dumps(result))
        result=manage_hosts('check',self.root,url='https://unapproved.example.invalid/a')
        self.assertEqual('NEEDS_CONFIGURATION',result['state'])
        self.assertFalse(result['allowed'])

    def test_add_requires_explicit_host_preserves_all_other_fields_and_is_idempotent(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('no network')):
            result=manage_hosts('add-approved-host',self.root,host='Approved.Example.Invalid')
        self.assertTrue(result['changed'])
        self.assertEqual({**self.data,'download_hosts':['existing.example.invalid','approved.example.invalid']},
                         json.loads(self.path.read_text(encoding='utf-8')))
        before=self.path.read_bytes()
        self.assertFalse(manage_hosts('add-approved-host',self.root,host='approved.example.invalid')['changed'])
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([],list(self.path.parent.glob('.download-hosts-*')))

    def test_wildcards_urls_credentials_and_ports_are_not_approved(self):
        before=self.path.read_bytes()
        for host in (None,'*.example.invalid','https://example.invalid/a?token=SECRET',
                     'user:SECRET@example.invalid','example.invalid:443','bad host','-bad.invalid'):
            with self.subTest(host=host),self.assertRaises(DownloadHostError) as error:
                manage_hosts('add-approved-host',self.root,host=host)
            self.assertNotIn('SECRET',str(error.exception))
        self.assertEqual(before,self.path.read_bytes())

    def test_cli_sanitizes_invalid_signed_url_and_never_prints_config_fields(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=hosts_main(['check','--root',str(self.root),'--url','https://name:SECRET@example.invalid/a'])
        self.assertEqual(2,code)
        self.assertEqual('FAILED',json.loads(output.getvalue())['state'])
        self.assertNotIn('SECRET',output.getvalue())
        self.assertNotIn('DO-NOT-PRINT',output.getvalue())

    def test_invalid_policy_cannot_be_partially_modified(self):
        self.path.write_text(json.dumps({**self.data,'max_download_bytes':'invalid'}),encoding='utf-8')
        before=self.path.read_bytes()
        with self.assertRaises(DownloadHostError):
            manage_hosts('add-approved-host',self.root,host='approved.example.invalid')
        self.assertEqual(before,self.path.read_bytes())


if __name__=='__main__':unittest.main()
