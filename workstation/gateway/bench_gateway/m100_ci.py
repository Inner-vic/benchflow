"""Read only the version parameters of the selected, completed Jenkins build.

CI credentials have their own encrypted host scope. Never persist an entire
Jenkins response: unrelated parameters may contain credentials or portal keys.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .download_auth import _load_authorization, _save_basic, _basic

HOST = 'ci.ep.chehejia.com'
JOB = '/job/TRANSIT/job/XCU/job/SCM/job/CCU-ACORE-RELEASE-BUILD/'
OWNER = 'team-m100-ci'
REF = r'[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+@[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+'


def authorization(cfg):
    return _load_authorization(OWNER, Path(cfg['project_root']) /
                               'runtime/source-auth'/OWNER/'jenkins.json', HOST)


def configure(cfg, username, password, fetch=None):
    """Operator-only secure prompt entry. Verify before replacing a credential."""
    from .m100_auto_inputs import _read
    fetch=fetch or _read
    header=_basic(username,password)
    proof=json.loads(fetch('https://'+HOST+'/whoAmI/api/json',{'Authorization':header},65536,20))
    if proof.get('authenticated') is not True:raise ValueError('Jenkins authentication failed')
    _save_basic(OWNER,Path(cfg['project_root'])/'runtime/source-auth'/OWNER/'jenkins.json',
                HOST,username,password)
    return {'configured':True,'host':HOST,'scope':'M100 release build parameters'}


def build_url(reference, number):
    expected = 'https://' + HOST + JOB + str(number) + '/'
    if reference:
        p = urlsplit(reference)
        if (p.scheme != 'https' or p.netloc != HOST or p.query or p.fragment
                or p.path.rstrip('/') != JOB + str(number)):
            raise ValueError('build reference is not the selected release build')
    return expected


def parameters(document, number):
    if (type(document.get('number')) is not int or document['number'] != int(number)
            or document.get('building') is not False or document.get('result') != 'SUCCESS'):
        raise ValueError('build identity/state is not a completed successful release')
    selected = {}
    for action in document.get('actions', []):
        for param in action.get('parameters', []):
            name = param.get('name')
            if name not in ('APP_VERSION', 'LOCKED_LIBS'):
                continue
            if name in selected or not isinstance(param.get('value'), str):
                raise ValueError('duplicate or invalid CI parameter')
            selected[name] = param['value']
    if set(selected) != {'APP_VERSION', 'LOCKED_LIBS'}:
        raise ValueError('release version parameters missing')

    def refs(text, name):
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith(name + '/')]
        if any(not re.fullmatch(REF, line) for line in lines):
            raise ValueError('version parameter is not an exact Conan reference')
        return set(lines)

    baselocks = refs(selected['APP_VERSION'], 'conan_shared')
    if len(baselocks) != 1:
        raise ValueError('APP_VERSION must identify one conan_shared')
    baselock = baselocks.pop()
    if not baselock.endswith('@livc/release-schu'):
        raise ValueError('conan_shared is not the documented release-schu profile')
    pins = {}
    for name in ('infer_expert_sdk', 'function-message'):
        values = refs(selected['APP_VERSION'], name) | refs(selected['LOCKED_LIBS'], name)
        if len(values) != 1:
            raise ValueError('SDK/message version missing or conflicting')
        pins[name] = values.pop()
    uroad = refs(selected['APP_VERSION'], 'uroad_m100') | refs(selected['LOCKED_LIBS'], 'uroad_m100')
    if len(uroad) > 1:
        raise ValueError('conflicting M100 version')
    sensors = {}
    for name in ('xcamera','xlidar'):
        values = refs(selected['APP_VERSION'],name) | refs(selected['LOCKED_LIBS'],name)
        if len(values)>1:raise ValueError('conflicting sensor development package')
        if values:sensors[name]=values.pop()
    return {'build_number': int(number), 'baselock': baselock,
            'build_requirement_pins': pins, 'sensor_development_pins': sensors,
            'uroad_ref': next(iter(uroad), None)}


def read(cfg, reference, number, fetch):
    from .m100_auto_inputs import InputError
    url = build_url(reference, number)
    auth = authorization(cfg)
    if not auth:
        raise InputError('M100_CI_AUTH_REQUIRED', '读取本次构建参数需要 Jenkins 只读认证，尚未编译。',
                         '请管理员配置一次 Jenkins 认证；不会使用旧构建参数。')
    try:
        body = fetch(url + 'api/json?tree=number,building,result,actions[parameters[name,value]]',
                     {'Authorization': auth}, 2 * 1024**2, 25)
        result = parameters(json.loads(body), number)
        return dict(result, build_reference=url)
    except InputError:
        raise
    except Exception as exc:
        code = getattr(exc, 'code', None)
        message = ('本次 Jenkins 构建记录不存在或当前账号不可见，未使用其他版本参数。' if code == 404
                   else '读取本次 Jenkins 构建参数失败，未启动编译。')
        raise InputError('M100_CI_PARAMETERS_UNAVAILABLE', message,
                         '请核对构建链接及只读权限；历史记录已清理时请提供配套 M100 成品，不猜测版本。') from None


def verify_pins(ci, document):
    for name, expected in {**ci['build_requirement_pins'],**ci.get('sensor_development_pins',{})}.items():
        actual = {n['ref'].split('#')[0] for n in document['graph_lock']['nodes'].values()
                  if n.get('context', 'host') == 'host' and n.get('ref', '').startswith(name + '/')}
        if actual != {expected}:
            raise ValueError('Jenkins dependency differs from the APP integration lock: ' + name)
    if ci.get('uroad_ref'):
        actual = {n['ref'].split('#')[0] for n in document['graph_lock']['nodes'].values()
                  if n.get('context', 'host') == 'host' and n.get('ref', '').startswith('uroad_m100/')}
        if actual != {ci['uroad_ref']}:
            raise ValueError('Jenkins M100 differs from the APP integration lock')
