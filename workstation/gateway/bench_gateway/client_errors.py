"""Public error categories only: never persist CLI output or credentials."""
from .core import GatewayError

MESSAGES={
 'CODEX_AUTH_REQUIRED':'Windows解析电脑的Codex登录已失效。请管理员在该电脑打开部署向导，选择“登录Codex”，完成后重新提交本条请求；原材料保留。',
 'CODEX_PERMISSION_DENIED':'Codex执行权限未通过，请管理员运行部署向导诊断；不会自动修改工具授权。',
 'CODEX_TIMEOUT':'Codex解析超时，原材料保留，未提交台架；请稍后重试。',
 'PARSER_FAILED':'个人电脑上的Codex解析未完成；材料保留，未执行台架。'}

class ParserFailure(GatewayError):
    def __init__(self,code):
        self.code=code if code in MESSAGES else 'PARSER_FAILED'
        super().__init__(MESSAGES[self.code])

def classify(text):
    text=text.lower()
    if any(s in text for s in ('refresh_token_reused','refresh_token_expired','token_expired','invalid_grant',
       'not logged in','please sign in again','please log in','access token could not be refreshed','authentication required',
       'authentication failed','unauthorized','invalid_api_key')):return 'CODEX_AUTH_REQUIRED'
    if any(s in text for s in ('permission denied','approval required','access was denied')):return 'CODEX_PERMISSION_DENIED'
    return 'PARSER_FAILED'
