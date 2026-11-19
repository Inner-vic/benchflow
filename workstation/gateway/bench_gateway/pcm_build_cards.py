"""Two durable per-mode result cards; notification retries never execute builds."""
import hashlib
import html


def md(value): return {'tag': 'markdown', 'content': value}


def panel(elements, color=None):
    return {'tag': 'column_set', 'flex_mode': 'none',
        **({'background_style': color+'-50'} if color else {}),
        'columns': [{'tag': 'column', 'width': 'weighted', 'weight': 1, 'padding': '8px', 'elements': elements}]}


def theme(state):
    return 'green' if state == 'SUCCEEDED' else 'red' if state in ('FAILED', 'NEEDS_REVIEW') else 'blue'


def card(title, state, content):
    color = theme(state)
    return {'schema': '2.0', 'config': {'update_multi': True, 'enable_forward': False},
        'header': {'template': color, 'title': {'tag': 'plain_text', 'content': title}},
        'body': {'padding': '12px', 'elements': content}}


def render_parent(job):
    from .pcm_release import is_release
    if is_release(job['review']):
        return render_release(job)
    review = job['review'];params = review['parameters']['OFF']
    summary = '**'+html.escape(job['progress'])+'**'
    if job['error']: summary += '\n'+html.escape(job['error'])
    context = [md('SW'+params['BASE_LINE']+' · ID0111 · '+params['BUILDVARIANT']+' · OFF / ON'),
        md('**分支** '+html.escape(review['source']['branch'])+'\n**Commit** `'+review['source']['commit']+'`')]
    if job.get('publication_url'): context.append(md('[组件发布流水线]('+job['publication_url']+')'))
    return card('PCM 双版本构建', job['state'], [panel([md(summary)], theme(job['state'])),
        panel(context),
        md('两版结果在各自构建卡中更新。本次仅构建软件，不操作台架。'), md('构建任务 '+job['id'])])


def render_mode(job, mode):
    result = job['modes'].get(mode, {})
    state = result.get('state', 'QUEUED')
    if job['state'] in ('FAILED', 'NEEDS_REVIEW') and state != 'SUCCEEDED':
        state = job['state']
    title = 'PCM 双版本构建 · '+('普通版 OFF' if mode == 'OFF' else '评测版 ON')
    message = result.get('message') or ('已停止，尚未完成发包' if state in ('FAILED', 'NEEDS_REVIEW') else job['progress'])
    if not result and mode == 'ON' and job['modes'].get('OFF') and job['state'] == 'RUNNING':
        message = '等待提交评测版构建' if job['modes']['OFF'].get('state') == 'SUCCEEDED' else '等待普通版完成后开始'
    status = [md('**'+html.escape(message)+'**')]
    if job['error'] and state in ('FAILED', 'NEEDS_REVIEW'): status.append(md(html.escape(job['error'])))
    elements = [panel(status, theme(state))]
    params = job['review']['parameters'][mode]
    elements += [panel([md('SW'+params['BASE_LINE']+' · ID0111 · '+params['BUILDVARIANT']),
        md('**源码** '+html.escape(job['review']['source']['branch'])+' · `'+job['review']['source']['commit'][:12]+'`')])]
    links = []
    if result.get('app'):
        links += [md('**LIVC APP**\n['+html.escape(result['app']['name'])+']('+result['app']['url']+')'),
                     md('[集成 Lockfile]('+result['lock']['url']+')')]
    if result.get('build_url'): links.append(md('[Jenkins 构建]('+result['build_url']+')'))
    if job.get('publication_url'): links.append(md('[组件发布流水线]('+job['publication_url']+')'))
    if state == 'SUCCEEDED': links.append(md('APP、Lockfile及M100变体来源已核对；未做台架回放验证。'))
    if links: elements.append(panel(links))
    elements.append(md('构建任务 '+job['id']))
    return card(title, state, elements)


def render_release(job):
    review = job['review'];params = review['parameters']['APP']
    result = job['modes'].get('APP', {})
    summary = '**'+html.escape(job['progress'])+'**'
    if job.get('error'):
        summary += '\n'+html.escape(job['error'])
    context = [md('SW'+params['BASE_LINE']+' · ID0111 · '+params['BUILDVARIANT']),
               md('**源码** '+html.escape(review['source']['branch'])+' · `'+review['source']['commit']+'`')]
    if result.get('build_url'):
        context.append(md('[Jenkins 构建]('+result['build_url']+')'))
    if result.get('app'):
        context += [md('**LIVC APP**\n['+html.escape(result['app']['name'])+']('+result['app']['url']+')'),
                    md('[集成 Lockfile]('+result['lock']['url']+')'),
                    md('**APP SHA-256** `'+result['app']['sha256']+'`')]
    return card('Jenkins 发包', job['state'], [panel([md(summary)], theme(job['state'])),
        panel(context), md('正式卡片沿用原 Jenkins 通知配置。依赖替换、M100 准备和台架执行另行处理。'),
        md('构建任务 '+job['id'])])


def deliver(store, client, jobs, job):
    from .card_entities import send_owner, update
    from .pcm_build_plan import PROJECT
    from .pcm_release import is_release
    if is_release(job['review']):
        # Reuse the original private configuration card; the original Job owns
        # the formal group notification. Delivery retries do not create cards.
        update(store, client, job['message_id'], render_release(job), lambda: 10)
        jobs.delivered(job)
        return
    publication = jobs.step(job['id'], 'CI_PIPELINE')
    if publication and publication['state'] == 'DONE':
        job = dict(job, publication_url='https://gitlab.chehejia.com/'+PROJECT+'/-/pipelines/'+str(publication['result']['id']))
    # Stable send UUIDs and existing CardKit receipts prevent duplicate cards.
    for mode in ('OFF', 'ON'):
        key = hashlib.sha256(('pcm-build-result:'+job['owner']+':'+job['id']+':'+mode).encode()).hexdigest()[:32]
        value = render_mode(job, mode)
        mid, chat = send_owner(store, client, job['owner'], value, key, lambda: 10)
        if chat != job['session'].split(':')[1]: raise ValueError('构建结果私聊不匹配。')
        update(store, client, mid, value, lambda: 10)
    update(store, client, job['message_id'], render_parent(job), lambda: 10)
    jobs.delivered(job)
