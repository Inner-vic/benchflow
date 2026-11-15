import contextlib
import http.client
import io
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode

from bench_gateway.core import Store
from bench_gateway.download_auth import credential_status
from bench_gateway.download_auth_setup import make_server


class DownloadAuthSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = {'project_root': str(root), 'state_root': str(root / 'state'),
                    'download_hosts': ['repo.example.com']}
        self.user = 'ou_form_owner'
        Store(self.cfg['state_root']).allow(self.user)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.server = make_server(self.cfg, self.user, 'repo.example.com', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.port = self.server.server_address[1]
        self.origin = 'http://127.0.0.1:' + str(self.port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method='GET', path='/', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read().decode('utf-8'))
        conn.close()
        return result

    def form(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        return re.search(r'name="csrf" value="([^"]+)"', body).group(1)

    def post(self, fields=None, headers=None, path='/save'):
        fields = fields or {'csrf': self.form(), 'username': 'dummy-account', 'password': 'dummy-secret'}
        values = {'Origin': self.origin, 'Content-Type': 'application/x-www-form-urlencoded'}
        values.update(headers or {})
        return self.request('POST', path, urlencode(fields), values)

    def test_loopback_scope_no_secrets_and_response_security_headers(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(self.user, body)
        self.assertIn('repo.example.com', body)
        self.assertIn('type="password"', body)
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])

    def test_refresh_save_page_redirects_to_root_without_request_data(self):
        status, headers, body = self.request(path='/save')
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(body, '')
        self.assertEqual(self.request(path='/save', headers={'Host': 'evil.example.com'})[0], 403)

    def test_save_and_get_do_not_echo_secret_or_username_and_do_not_log(self):
        status, _, body = self.post()
        self.assertEqual(status, 200)
        self.assertIn('尚未验证下载', body)
        self.assertNotIn('dummy', body)
        self.assertTrue(credential_status(self.cfg, self.user, 'repo.example.com')['configured'])
        _, _, body = self.request()
        self.assertNotIn('dummy', body)
        self.assertEqual(self.output.getvalue(), '')

    def test_bad_host_rejected_for_get_and_post(self):
        self.assertEqual(self.request(headers={'Host': 'evil.example.com'})[0], 403)
        self.assertEqual(self.post(headers={'Host': 'evil.example.com'})[0], 403)

    def test_bad_origin_and_csrf_rejected(self):
        for origin in ['http://evil.example.com', 'null', self.origin + '.evil.example.com']:
            self.assertEqual(self.post(headers={'Origin': origin})[0], 403)
        self.assertEqual(self.post(fields={'csrf': 'wrong', 'username': 'dummy', 'password': 'dummy'})[0], 403)
        self.assertFalse(credential_status(self.cfg, self.user, 'repo.example.com')['configured'])

    def test_owner_or_host_cannot_be_changed_from_form(self):
        values = {'csrf': self.form(), 'username': 'dummy', 'password': 'dummy', 'user': 'ou_other_user'}
        self.assertEqual(self.post(fields=values)[0], 400)

    def test_invalid_password_error_never_echoes_input(self):
        status, _, body = self.post(fields={'csrf': self.form(), 'username': 'dummy', 'password': 'secret\nheader'})
        self.assertEqual(status, 400)
        self.assertNotIn('secret', body)
        self.assertNotIn('dummy', body)
        self.assertIn('账号或密码格式无效', body)

    def test_username_trimmed_password_preserved(self):
        from bench_gateway.download_auth import load_authorization
        import base64
        status, _, _ = self.post(fields={'csrf': self.form(), 'username': ' dummy-account ', 'password': ' dummy-password '})
        self.assertEqual(status, 200)
        decoded = base64.b64decode(load_authorization(self.cfg, self.user, 'repo.example.com')[6:]).decode()
        self.assertEqual(decoded, 'dummy-account: dummy-password ')

    def test_storage_failure_reason_is_fixed_and_secret_free(self):
        with mock.patch('bench_gateway.download_auth_setup.save_basic', side_effect=OSError('dummy-sensitive-error')):
            status, _, body = self.post()
        self.assertEqual(status, 400)
        self.assertIn('本机安全保存失败', body)
        self.assertNotIn('dummy', body)

    def test_request_body_limit_and_content_type(self):
        status, _, body = self.post(fields={'csrf': self.form(), 'username': 'dummy', 'password': 'x' * 32769})
        self.assertEqual(status, 400)
        self.assertIn('提交内容过长或长度无效', body)
        status, _, body = self.post(headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        self.assertIn('请求格式不支持', body)
        self.assertFalse(credential_status(self.cfg, self.user, 'repo.example.com')['configured'])

    def test_duplicate_fields_and_unknown_paths(self):
        headers = {'Origin': self.origin, 'Content-Type': 'application/x-www-form-urlencoded'}
        self.assertEqual(self.request('POST', '/save', 'csrf=x&username=a&password=b&password=c', headers)[0], 400)
        self.assertEqual(self.request(path='/other')[0], 404)
        self.assertEqual(self.post(path='/other')[0], 403)


if __name__ == '__main__':
    unittest.main()
