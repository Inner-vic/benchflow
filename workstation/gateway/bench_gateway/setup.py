"""Local-only credentials form. Secrets are never placed in argv or chat output."""
import html
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import parse_qs

from .config import save
from .core import GatewayError,Store,owner_key


def cc_config(cfg):
    root=Path(cfg['project_root'])
    q=lambda s:json.dumps(str(s),ensure_ascii=False)
    text=f'''# Generated locally; real App Secret is passed by environment.
language = "zh"
data_dir = {q(root/'runtime/cc-connect')}
[log]
level = "info"
[[projects]]
name = {q(cfg['cc_project'])}
reply_footer = false
admin_from = ""
disabled_commands = ["*"]
reset_on_idle_mins = 0
[projects.display]
mode = "quiet"
thinking_messages = false
tool_messages = false
show_context_indicator = false
reply_footer = false
hide_agent_footer = true
[projects.agent]
type = "codex"
[projects.agent.options]
cmd = [{q(sys.executable)}, {q(root/'gateway_agent.py')}]
work_dir = {q(root)}
backend = "exec"
mode = "suggest"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = {q(cfg['feishu_app_id'])}
app_secret = "${{BENCH_FEISHU_APP_SECRET}}"
# Feishu availability scope governs admission; owner directories are provisioned automatically.
allow_from = "*"
# v1.5.0 applies allow_chat to group messages only. No real chat can match this sentinel.
allow_chat = "__gateway_private_only__"
share_session_in_channel = false
thread_isolation = false
'''
    path=root/'config/cc-connect.toml'
    path.write_text(text,encoding='utf-8')
    return path


def save_secret(cfg,app_id,secret,users):
    if app_id!=cfg['feishu_app_id']:
        raise GatewayError('App ID 与本次指定机器人不一致')
    if not secret or len(secret)>512 or any(c in secret for c in '\r\n\x00'):
        raise GatewayError('App Secret 格式无效')
    for user in users:owner_key(user)
    path=Path(cfg['project_root'])/'runtime/credentials.json'
    save(path,{'app_id':app_id,'app_secret':secret})
    if os.name=='nt':
        import getpass
        result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',getpass.getuser()+':F'],capture_output=True)
        if result.returncode:
            path.unlink(missing_ok=True)
            raise GatewayError('无法限制凭据文件权限，未保留 App Secret')
    else:path.chmod(0o600)
    store=Store(cfg['state_root'])
    for user in users:store.allow(user)
    cc_config(cfg)


def serve(cfg,port=18765):
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,code,body,ctype='text/html; charset=utf-8'):
            raw=body.encode();self.send_response(code);self.send_header('Content-Type',ctype)
            self.send_header('Cache-Control','no-store');self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{port}',f'localhost:{port}'):
                return self.send(403,'Forbidden')
            if self.path!='/':return self.send(404,'Not Found')
            configured=(Path(cfg['project_root'])/'runtime/credentials.json').exists()
            body=(Path(cfg['project_root'])/'web/setup.html').read_text(encoding='utf-8')
            body=body.replace('__TOKEN__',html.escape(token)).replace('__APP_ID__',html.escape(cfg['feishu_app_id'])).replace('__STATUS__','已保存凭据；重新提交将更新。' if configured else '尚未保存 App Secret。')
            self.send(200,body)
        def do_POST(self):
            origin=self.headers.get('Origin')
            if self.path!='/save' or origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
                return self.send(403,'Forbidden')
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<10000:raise GatewayError('请求长度无效')
                values=parse_qs(self.rfile.read(length).decode())
                if not secrets.compare_digest(values.get('token',[''])[0],token):return self.send(403,'Forbidden')
                app_id=values.get('app_id',[''])[0].strip();secret=values.get('app_secret',[''])[0].strip()
                users=[u.strip() for u in values.get('users',[''])[0].replace(',','\n').splitlines() if u.strip()]
                save_secret(cfg,app_id,secret,users)
                self.send(200,'<meta charset="utf-8"><p>已在本机保存凭据并生成连接配置，未向聊天发送凭据。</p><p>请返回 Codex 告知“已配置”，随后启动连接并发送一条测试消息。飞书用户首次使用时自动建立本人目录，无需另行开通。</p>')
            except Exception:
                self.send(400,'<meta charset="utf-8"><p>保存失败。请检查 App ID、App Secret 和 open_id 格式。凭据不会显示在此页面。</p>')
    print(f'本机配置页：http://127.0.0.1:{port}/',flush=True)
    HTTPServer(('127.0.0.1',port),Handler).serve_forever()


def launch_command(cfg):
    root=Path(cfg['project_root']);credentials=root/'runtime/credentials.json'
    if not credentials.exists():raise GatewayError('请先在本机配置页填写已有机器人的 App Secret')
    data=json.loads(credentials.read_text(encoding='utf-8'))
    if data['app_id']!=cfg['feishu_app_id']:raise GatewayError('机器人身份不一致')
    env=os.environ.copy();env['BENCH_FEISHU_APP_SECRET']=data['app_secret'];env['PYTHONUTF8']='1'
    config=cc_config(cfg)
    return [cfg['cc_binary'],'--config',str(config)],env


def launch(cfg):
    argv,env=launch_command(cfg)
    return subprocess.call(argv,cwd=cfg['project_root'],env=env)
