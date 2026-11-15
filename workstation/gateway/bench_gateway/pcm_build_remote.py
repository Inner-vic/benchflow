"""Fixed-host GitLab/Jenkins/JFrog API adapter. Credentials remain in memory."""
import http.cookiejar
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from .pcm_build_jobs import BuildFailure, Uncertain
from .pcm_build_plan import GITLAB, JOB, PROJECT


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirect refused')


class Remote:
    def __init__(self, cfg, owner):
        from .m100_source_auth import load
        from .m100_ci import authorization
        from .download_auth import load_authorization
        self.headers = {
            'gitlab.chehejia.com': {'PRIVATE-TOKEN': load(cfg)},
            'ci.ep.chehejia.com': {'Authorization': authorization(cfg)},
            'artifactory.ep.chehejia.com': {'Authorization': load_authorization(cfg, owner, 'artifactory.ep.chehejia.com')}}
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, url, *, method='GET', body=None, content_type=None, limit=8*1024*1024):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in self.headers
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
            raise BuildFailure('平台地址不在允许范围。')
        headers = dict(self.headers[parsed.hostname])
        if not all(headers.values()): raise Uncertain('平台认证未配置，需管理员处理；未自动重新提交。')
        if content_type: headers['Content-Type'] = content_type
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers, data=body, method=method), timeout=20) as response:
                raw = response.read(limit+1)
                if len(raw) > limit: raise Uncertain('平台返回内容超出受控读取上限。')
                return raw, response.status, response.headers
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise Uncertain('平台认证失效或权限不足，请管理员检查；未自动重复提交。') from None
            raise

    def get(self, url, limit=8*1024*1024):
        return self.request(url, limit=limit)[0]

    def json(self, url):
        return json.loads(self.get(url))

    def post(self, url, value):
        # Only dedicated private commits/pipelines and read-only CI lint.
        allowed = GITLAB+'projects/'+PROJECT.replace('/', '%2F')
        if url not in {allowed+'/repository/commits', allowed+'/pipeline', allowed+'/ci/lint'}:
            raise BuildFailure('未允许的GitLab写入地址。')
        try:
            return json.loads(self.request(url, method='POST', body=json.dumps(value).encode(), content_type='application/json')[0])
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                raise BuildFailure('GitLab拒绝发布请求（HTTP '+str(error.code)+'），请管理员检查对应配置。') from None
            raise

    def submit_jenkins(self, params):
        headers = self.headers['ci.ep.chehejia.com']
        crumb = self.json('https://ci.ep.chehejia.com/crumbIssuer/api/json')
        field, value = crumb.get('crumbRequestField'), crumb.get('crumb')
        if not isinstance(field, str) or not re.fullmatch('[A-Za-z0-9_-]+', field) or not isinstance(value, str):
            raise BuildFailure('Jenkins CSRF认证未就绪，未提交。')
        body = urlencode({k: str(v).lower() if type(v) is bool else v for k, v in params.items()}).encode()
        request = urllib.request.Request(JOB+'buildWithParameters', data=body,
            headers={**headers, field: value, 'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        try:
            with self.opener.open(request, timeout=20) as response:
                location = response.headers.get('Location', '')
                if response.status != 201 or not re.fullmatch(r'https://ci\.ep\.chehejia\.com/queue/item/[0-9]+/', location):
                    raise Uncertain('Jenkins未返回可核验的排队编号，不能重复提交。')
                return {'queue_url': location, 'queue_id': int(location.rstrip('/').split('/')[-1])}
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                raise BuildFailure('Jenkins拒绝构建请求（HTTP '+str(error.code)+'），请管理员检查。') from None
            raise
