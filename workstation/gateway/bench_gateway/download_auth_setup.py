"""Loopback-only configuration page for one owner and one download host."""
import argparse
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
from urllib.parse import parse_qs

from .config import load
from .core import GatewayError
from .download_auth import credential_status, save_basic


def make_server(cfg, user, host, port=18766):
    credential_status(cfg, user, host)
    csrf = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def origins(self):
            actual_port = self.server.server_address[1]
            return {'http://127.0.0.1:' + str(actual_port), 'http://localhost:' + str(actual_port)}

        def valid_host(self):
            return 'http://' + self.headers.get('Host', '') in self.origins()

        def send(self, code, body):
            raw = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, 'Forbidden')
            if self.path == '/save':
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path != '/':
                return self.send(404, 'Not Found')
            try:
                configured = credential_status(cfg, user, host)['configured']
            except GatewayError:
                return self.send(503, '本机下载认证配置不可用，请检查用户及本机配置')
            state = '已保存；此页面不验证下载，再次保存将更新凭据。' if configured else '尚未配置。'
            body = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>软件仓库下载认证</title>
<h1>软件仓库下载认证</h1><p>仅对飞书用户 <code>__USER__</code> 与 HTTPS 域名 <code>__HOST__</code> 生效。</p>
<p>__STATE__</p><p>凭据加密保存在入口服务所在工作站的私有目录。密码不会显示在聊天、任务单或日志中。</p>
<form method="post" action="/save"><input type="hidden" name="csrf" value="__CSRF__">
<p><label>JFrog 用户名 <input name="username" autocomplete="username" maxlength="256" required></label></p>
<p><label>JFrog 密码 <input type="password" name="password" autocomplete="current-password" maxlength="4096" required></label></p>
<button type="submit">保存下载凭据</button></form></html>'''
            for key, value in {'__USER__': user, '__HOST__': host, '__STATE__': state, '__CSRF__': csrf}.items():
                body = body.replace(key, html.escape(value, quote=True))
            return self.send(200, body)

        def do_POST(self):
            if (self.path != '/save' or not self.valid_host()
                    or self.headers.get('Origin') not in self.origins()):
                return self.send(403, 'Forbidden')
            stage = 'content-type'
            try:
                if self.headers.get('Transfer-Encoding') is not None:
                    raise ValueError()
                if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
                    raise ValueError()
                stage = 'body-length'
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768:
                    raise ValueError()
                stage = 'parse-fields'
                values = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                                  strict_parsing=True, max_num_fields=3)
                if set(values) != {'csrf', 'username', 'password'} or any(len(value) != 1 for value in values.values()):
                    raise ValueError()
                if not secrets.compare_digest(values['csrf'][0], csrf):
                    return self.send(403, 'Forbidden')
                stage = 'secure-storage'
                try:
                    save_basic(cfg, user, host, values['username'][0].strip(), values['password'][0])
                except GatewayError as error:
                    if str(error) == '下载账号或密码格式无效':
                        stage = 'credential-format'
                    raise
            except Exception:
                reasons = {
                    'content-type': '请求格式不支持，请使用此页面表单提交。',
                    'body-length': '提交内容过长或长度无效，请重新填写。',
                    'parse-fields': '提交字段不完整或重复，请刷新页面后重新填写。',
                    'credential-format': '账号或密码格式无效，请检查是否完整；密码不能包含换行。',
                    'secure-storage': '本机安全保存失败，请联系管理员检查凭据目录权限及当前 Windows 账户。',
                }
                return self.send(400, '保存失败：' + reasons[stage] + '凭据不会显示在此页面。')
            return self.send(200, '下载凭据已在本机私有目录保存，尚未验证下载。后续由工作站直接下载，不经过 CC Connect 传输软件包。')
    server = HTTPServer(('127.0.0.1', port), Handler)
    server.timeout = 1
    return server


def serve(cfg, user, host, port=18766):
    server = make_server(cfg, user, host, port)
    print('本机下载认证配置页：http://127.0.0.1:' + str(server.server_address[1]) + '/', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='Configure one Feishu owner’s download authentication locally')
    parser.add_argument('--root', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=18766)
    args = parser.parse_args()
    try:
        serve(load(args.root), args.user, args.host, args.port)
    except (GatewayError, OSError, ValueError):
        parser.exit(1, '本机下载认证配置页未启动，请检查用户、域名、端口及目录权限。\n')


if __name__ == '__main__':
    main()
