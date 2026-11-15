"""Resumable software release engine. Contains no SSH or bench execution path."""
import hashlib
import json
import re
import time
import urllib.error
from urllib.parse import quote, urlencode, urlsplit

from .pcm_build_jobs import BuildFailure, Uncertain, ReadPending
from .pcm_build_plan import GITLAB, JOB, PROJECT, checked

API = GITLAB+'projects/'+quote(PROJECT, safe='')
ARTIFACT = 'https://artifactory.ep.chehejia.com/artifactory/'


def parameters(build):
    out = {}
    for action in build.get('actions', []):
        for p in action.get('parameters', []):
            if p['name'] in out: raise Uncertain('Jenkins存在重复参数，需核验。')
            out[p['name']] = p.get('value')
    return out


def matches(build, expected):
    actual = parameters(build)
    return all(k in actual and actual[k] == v for k, v in expected.items())


def artifact_uri(uri):
    p = urlsplit(uri)
    if (p.scheme != 'https' or p.hostname != 'artifactory.ep.chehejia.com'
            or p.port not in (None, 443) or p.username or p.password or p.query or p.fragment
            or '..' in p.path.split('/')
            or not p.path.startswith('/artifactory/api/storage/xcu-acore-test-local/Acore/Civu/')):
        raise Uncertain('构建产物地址来源不匹配。')
    return 'https://artifactory.ep.chehejia.com'+p.path


class Engine:
    def __init__(self, jobs, remote, verify_recipe):
        self.jobs, self.remote, self.verify_recipe = jobs, remote, verify_recipe

    def checkpoint(self, job, name, value):
        with self.jobs.store.connect() as c:
            c.execute("INSERT OR IGNORE INTO pcm_build_steps VALUES(?,?,'DONE','{}',?,?)",
                      (job['id'], name, json.dumps(value, ensure_ascii=False), time.time()))

    def saved(self, job, name):
        step = self.jobs.step(job['id'], name)
        return step['result'] if step and step['state'] == 'DONE' else None

    def progress(self, job, text, mode=None, result=None):
        current = self.jobs.get(job['id'])
        if current['progress'] != text or (mode and current['modes'].get(mode) != result):
            self.jobs.update(job['id'], progress=text, mode=mode, result=result)

    def preflight(self, job):
        review = checked(job['review'])
        definition = self.remote.json(JOB+'api/json?'+urlencode({'tree': 'property[parameterDefinitions[name,choices,defaultParameterValue[value]]]'}))
        fields = {v['name']: v for prop in definition.get('property', []) for v in prop.get('parameterDefinitions', [])}
        covered = set(review['parameters']['OFF'])
        # Jenkins explicitly documents IMPORTANT as an inert link to its usage
        # guide. It is not a release parameter; do not omit any unknown field.
        notice_values = (None, '', 'https://example.feishu.cn/docx/DEMO00000000000000000000002')
        if (covered-set(fields) or set(fields)-covered-{'IMPORTANT'}
                or fields.get('IMPORTANT', {}).get('defaultParameterValue', {}).get('value') not in notice_values):
            raise BuildFailure('Jenkins参数定义已变化，请管理员核对；未创建发布分支或提交构建。')
        for name in ('CONTROLLER', 'BUILDVARIANT'):
            if fields[name].get('choices') and review['parameters']['OFF'][name] not in fields[name]['choices']:
                raise BuildFailure('Jenkins不接受本次目标或构建类型，未提交。')
        source = review['source']
        head = self.remote.json(API+'/repository/branches/'+quote(source['branch'], safe=''))
        if head.get('commit', {}).get('id') != source['commit']:
            raise BuildFailure('源码分支已更新，请重新校验并确认；未提交发包。')
        for proof in review['recipe_evidence'].values():
            ref = proof['ref']
            actual = self.verify_recipe(self.remote, ref, proof['recipe_revision'])
            if (actual.get('state') != 'VERIFIED' or actual.get('recipe_sha256') != proof['recipe_sha256']
                    or actual.get('ref') != ref or actual.get('recipe_revision') != proof['recipe_revision']):
                raise BuildFailure('组件配方来源已变化，请重新校验。')
        lint = self.remote.post(API+'/ci/lint', {'content': review['payload']['.gitlab-ci.yml'],
            'ref': source['commit'], 'include_jobs': True, 'include_merged_yaml': True})
        if lint.get('valid') is not True or {j['name'] for j in lint.get('jobs', [])} != {'publish-pcm-recipes'}:
            raise BuildFailure('独立CI配置校验未通过，未创建分支或启动流水线。')
        return {'valid': True, 'job_parameters': sorted(covered), 'ignored_notice_field': 'IMPORTANT' if 'IMPORTANT' in fields else None,
                'merged_sha256': hashlib.sha256(lint.get('merged_yaml', '').encode()).hexdigest()}

    def commit_result(self, review, sha):
        if not re.fullmatch('[a-f0-9]{40}', sha or ''): raise Uncertain('发布提交编号无效。')
        head = self.remote.json(API+'/repository/branches/'+quote(review['private_branch'], safe=''))
        if head.get('commit', {}).get('id') != sha: raise Uncertain('独立发布分支已被改变。')
        commit = self.remote.json(API+'/repository/commits/'+sha)
        if commit.get('parent_ids') != [review['source']['commit']]: raise Uncertain('发布提交不基于已确认源码。')
        for path, text in review['payload'].items():
            raw = self.remote.get(API+'/repository/files/'+quote(path, safe='')+'/raw?ref='+sha)
            if raw != text.encode(): raise Uncertain('发布文件与已确认快照不一致。')
        # Verify that no business source was modified by the private commit.
        changes = self.remote.json(API+'/repository/commits/'+sha+'/diff?per_page=100')
        if {d['new_path'] for d in changes} != set(review['payload']):
            raise Uncertain('独立发布提交的文件范围不一致。')
        return {'sha': sha, 'branch': review['private_branch']}

    def commit(self, job):
        review = job['review'];branch = review['private_branch']
        def perform():
            actions = [{'action': 'update' if path == '.gitlab-ci.yml' and review['ci_file_exists'] else 'create',
                        'file_path': path, 'content': text} for path, text in review['payload'].items()]
            reply = self.remote.post(API+'/repository/commits', {'branch': branch,
                'start_sha': review['source']['commit'], 'commit_message': 'PCM Bot '+review['release_id']+': private ON/OFF recipes',
                'actions': actions})
            return self.commit_result(review, reply.get('id'))
        def recover(step):
            try: head = self.remote.json(API+'/repository/branches/'+quote(branch, safe=''))
            except urllib.error.HTTPError as error:
                if error.code == 404: return None
                raise
            return self.commit_result(review, head.get('commit', {}).get('id'))
        return self.jobs.mutation(job, 'CI_COMMIT', {'branch': branch, 'review_sha': job['review_sha']}, perform, recover)

    def pipeline(self, job, commit):
        review = job['review']
        def validate(p):
            if (type(p.get('id')) is not int or p.get('sha') != commit['sha']
                    or p.get('ref') != review['private_branch']):
                raise Uncertain('CI流水线身份与发布提交不一致。')
            return {'id': p['id'], 'sha': p['sha'], 'ref': p['ref']}
        def perform():
            head = self.remote.json(API+'/repository/branches/'+quote(review['private_branch'], safe=''))
            if head.get('commit', {}).get('id') != commit['sha']:
                raise BuildFailure('发布分支在启动前已变化，未启动新流水线。')
            return validate(self.remote.post(API+'/pipeline', {'ref': review['private_branch']}))
        def recover(step):
            items = self.remote.json(API+'/pipelines?'+urlencode({'ref': review['private_branch'],
                'sha': commit['sha'], 'source': 'api', 'per_page': 100}))
            if len(items) != 1: return None
            return validate(self.remote.json(API+'/pipelines/'+str(items[0]['id'])))
        return self.jobs.mutation(job, 'CI_PIPELINE', {'branch': review['private_branch'], 'commit': commit['sha']}, perform, recover)

    def publication(self, job, pipeline):
        p = self.remote.json(API+'/pipelines/'+str(pipeline['id']))
        if p.get('sha') != pipeline['sha'] or p.get('ref') != pipeline['ref']:
            raise Uncertain('CI流水线来源已变化。')
        if p.get('status') in ('created', 'waiting_for_resource', 'preparing', 'pending', 'running'):
            self.progress(job, '正在发布ON/OFF独立组件配方')
            return None
        if p.get('status') != 'success': raise BuildFailure('组件配方发布失败，尚未提交Jenkins；请查看独立发布流水线。')
        jobs = self.remote.json(API+'/pipelines/'+str(pipeline['id'])+'/jobs?per_page=100')
        if len(jobs) != 1 or jobs[0].get('name') != 'publish-pcm-recipes' or jobs[0].get('status') != 'success':
            raise Uncertain('发布流水线出现非预期任务，已停止后续派发。')
        receipt = self.remote.json(API+'/jobs/'+str(jobs[0]['id'])+'/artifacts/pcm-publication-receipt.json')
        if receipt.get('status') != 'REVIEWED_RECIPES_VERIFIED': raise BuildFailure('组件发布收据未完整，未提交Jenkins。')
        evidence = {p['mode']: p for p in receipt.get('verified', [])}
        if set(evidence) != {'OFF', 'ON'}: raise Uncertain('发布范围与已确认的ON/OFF不一致。')
        for mode in ('OFF', 'ON'):
            expected = job['review']['manifest']['components'][mode];proof = evidence[mode]
            if proof['ref'] != expected['ref'] or proof['recipe_sha256'] != expected['recipe_sha256']:
                raise Uncertain('发布配方与已确认内容不同。')
            actual = self.verify_recipe(self.remote, proof['ref'], proof['recipe_revision'])
            if actual.get('state') != 'VERIFIED' or actual.get('recipe_sha256') != expected['recipe_sha256']:
                raise Uncertain('远端配方回读校验失败。')
        return evidence

    def find_jenkins(self, params, *, queue_id=None):
        tree = 'number,building,result,url,queueId,timestamp,actions[parameters[name,value]]'
        listing = self.remote.json(JOB+'api/json?'+urlencode({'tree': 'builds['+tree+']{0,100}'}))
        found = []
        for b in listing.get('builds', []):
            if matches(b, params) and (queue_id is None or b.get('queueId') == queue_id):
                found.append({'build_url': b['url'], 'queue_id': b['queueId']})
        queue = self.remote.json('https://ci.ep.chehejia.com/queue/api/json?'+urlencode({'tree': 'items[id,url,task[url],actions[parameters[name,value]]]' }))
        for item in queue.get('items', []):
            if (item.get('task', {}).get('url') == JOB and matches(item, params)
                    and (queue_id is None or item['id'] == queue_id)):
                found.append({'queue_url': 'https://ci.ep.chehejia.com/queue/item/'+str(item['id'])+'/', 'queue_id': item['id']})
        # A queue item and its executable may overlap briefly; that is one dispatch.
        by_id = {}
        for item in found:
            by_id[item['queue_id']] = dict(by_id.get(item['queue_id'], {}), **item)
        return next(iter(by_id.values())) if len(by_id) == 1 else None

    def submit(self, job, mode):
        params = job['review']['parameters'][mode]
        return self.jobs.mutation(job, 'JENKINS_'+mode,
            {'mode': mode, 'parameters_sha': hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()},
            lambda: self.remote.submit_jenkins(params), lambda step: self.find_jenkins(params))

    def poll_build(self, job, mode, dispatch):
        params = job['review']['parameters'][mode]
        if dispatch.get('build_url'):
            url = dispatch['build_url']
        else:
            if not re.fullmatch(r'https://ci\.ep\.chehejia\.com/queue/item/[0-9]+/', dispatch.get('queue_url', '')):
                raise Uncertain('Jenkins队列地址无效。')
            try: q = self.remote.json(dispatch['queue_url']+'api/json')
            except urllib.error.HTTPError as error:
                if error.code != 404: raise
                found = self.find_jenkins(params, queue_id=dispatch['queue_id'])
                if not found or not found.get('build_url'): raise Uncertain('原队列已不可查，构建身份尚未确认；不会重新提交。')
                q = {'executable': {'url': found['build_url']}}
            if q.get('cancelled'): raise BuildFailure(mode+'版构建已被取消，不自动重新提交。')
            if not q.get('executable'):
                self.progress(job, mode+'版已进入Jenkins队列', mode,
                    {'state': 'QUEUED', 'message': '等待Jenkins构建', 'queue_id': dispatch['queue_id']})
                return None
            url = q['executable']['url']
        if not re.fullmatch(re.escape(JOB)+r'[0-9]+/', url): raise Uncertain('构建地址不属于原Jenkins任务。')
        number = int(url.rstrip('/').split('/')[-1])
        build = self.remote.json(url+'api/json?'+urlencode({'tree': 'number,building,result,url,queueId,timestamp,actions[parameters[name,value]]'}))
        if (build.get('number') != number or build.get('url') != url or build.get('queueId') != dispatch['queue_id']
                or not matches(build, params)):
            raise Uncertain('Jenkins实际参数或构建身份与冻结输入不同。')
        common = {'build_url': url, 'number': number, 'queue_id': dispatch['queue_id']}
        if build.get('building'):
            self.progress(job, mode+'版正在编译与打包', mode,
                dict(common, state='RUNNING', message='正在编译M100并打包APP'))
            return None
        if build.get('result') != 'SUCCESS':
            self.jobs.update(job['id'], mode=mode, result=dict(common, state='FAILED', message='Jenkins构建失败（'+str(build.get('result'))+'）'))
            raise BuildFailure(mode+'版Jenkins构建未成功，已停止后续发包；请查看对应构建日志。')
        return common

    def artifacts(self, job, mode, build, published):
        params = job['review']['parameters'][mode];number = build['number']
        product = job['review']['baseline']['product_variant']
        if product not in ('ULTRA', 'MAX'): raise Uncertain('产品配置未核验。')
        prefix = 'CCU_LIVC_AVM-'+params['BUILDVARIANT']+'-SW'+params['BASE_LINE']+'_'+product+'_ID0111.'
        outputs = {}
        for kind, suffix in [('app', '.app.tar.gz'), ('lock', '.lock')]:
            response = self.remote.json(ARTIFACT+'api/search/artifact?'+urlencode({'name': '*-'+str(number)+suffix, 'repos': 'xcu-acore-test-local'}))
            candidates = []
            for item in response.get('results', []):
                uri = artifact_uri(item['uri']);name = uri.rsplit('/', 1)[-1]
                if name.startswith(prefix) and name.endswith('-'+str(number)+suffix): candidates.append((uri, name))
            candidates = sorted(set(candidates))
            if not candidates:
                observed = self.saved(job, mode+'_FINISHED') or {}
                if time.time()-observed.get('observed_at', 0) < 600:
                    raise ReadPending('Jenkins已成功，正在等待'+mode+'版产物索引')
                raise Uncertain('成功构建的'+kind+'产物仍未找到，请核对原构建；不重复发包。')
            if len(candidates) != 1: raise Uncertain('成功构建的'+kind+'产物存在歧义，请核对原构建；不重复发包。')
            uri, name = candidates[0];meta = self.remote.json(uri)
            sha = meta.get('checksums', {}).get('sha256')
            if not re.fullmatch('[a-f0-9]{64}', sha or '') or int(meta.get('size', 0)) <= 0:
                raise Uncertain('构建产物校验信息不完整。')
            outputs[kind] = {'name': name, 'url': uri.replace('/api/storage/', '/', 1), 'sha256': sha, 'bytes': int(meta['size'])}
        if outputs['lock']['name'] != outputs['app']['name'][:-len('.app.tar.gz')]+'.lock':
            raise Uncertain('APP与Lockfile不是同一构建产物。')
        raw = self.remote.get(outputs['lock']['url'], 4*1024*1024)
        if hashlib.sha256(raw).hexdigest() != outputs['lock']['sha256']: raise Uncertain('Lockfile校验和不一致。')
        nodes = list(json.loads(raw)['graph_lock']['nodes'].values())
        expected = published[mode]
        m100 = [n for n in nodes if n.get('ref', '').startswith('uroad_m100/') and n.get('context', 'host') == 'host']
        if (len(m100) != 1 or m100[0].get('ref') != expected['ref']+'#'+expected['recipe_revision']
                or not m100[0].get('package_id') or not m100[0].get('prev')):
            raise Uncertain('实际APP未锁定本次M100变体和二进制。')
        all_refs = {n.get('ref', '').split('#')[0] for n in nodes}
        pins = [line for name in ('APP_VERSION', 'LOCKED_LIBS') for line in params[name].splitlines() if line and not line.startswith('conan_shared/')]
        if not set(pins) <= all_refs: raise Uncertain('实际Lockfile未包含全部已确认组件。')
        for proof in job['review']['recipe_evidence'].values():
            ref = proof['ref']
            if ref.startswith(('conan_shared/', 'uroad_m100/')): continue
            actual = [n for n in nodes if n.get('ref', '').split('#')[0] == ref]
            if not actual or any(n['ref'] != ref+'#'+proof['recipe_revision'] for n in actual):
                raise Uncertain('实际依赖配方修订与确认记录不一致，请核对构建；不会自动重新发包。')
        return dict(build, **outputs, state='SUCCEEDED', message='构建完成，APP与Lockfile已核对',
                    m100={'ref': expected['ref'], 'recipe_revision': expected['recipe_revision'],
                          'package_id': m100[0]['package_id'], 'prev': m100[0]['prev'], 'recipe_compile_mode': mode})

    def advance(self, job):
        checked(job['review'])
        if not self.saved(job, 'PREFLIGHT'):
            self.progress(job, '正在核对已确认源码与独立CI配置')
            self.checkpoint(job, 'PREFLIGHT', self.preflight(job));return
        commit = self.commit(job)
        pipeline = self.pipeline(job, commit)
        published = self.saved(job, 'PUBLISHED')
        if not published:
            published = self.publication(job, pipeline)
            if published is None: return
            self.checkpoint(job, 'PUBLISHED', published)
        for mode in ('OFF', 'ON'):
            done = self.saved(job, mode+'_DONE')
            if done:
                current = self.jobs.get(job['id'])['modes'].get(mode)
                if current != done: self.jobs.update(job['id'], mode=mode, result=done)
                continue
            dispatch = self.submit(job, mode)
            build = self.poll_build(job, mode, dispatch)
            if build is None: return
            self.checkpoint(job, mode+'_FINISHED', dict(build, observed_at=time.time()))
            verifying = dict(build, state='VERIFYING', message='Jenkins已成功，正在核对APP与Lockfile')
            if self.jobs.get(job['id'])['modes'].get(mode) != verifying:
                self.jobs.update(job['id'], mode=mode, result=verifying, progress=mode+'版正在核对产物')
            output = self.artifacts(job, mode, build, published)
            self.checkpoint(job, mode+'_DONE', output)
            self.jobs.update(job['id'], mode=mode, result=output, progress=mode+'版构建完成')
            return
        self.jobs.update(job['id'], state='SUCCEEDED', progress='ON/OFF两版APP均已完成发包')
