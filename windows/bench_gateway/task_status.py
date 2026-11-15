"""Monotone business phase observations; no model, I/O to a bench or estimated success."""
import time


STAGES={'小场景预检中':(10,'准备台架软件'),'正式回灌中':(30,'正式回灌'),
        '小场景预检通过':(25,'小场景通过'),'回灌完成，保存本人装配包':(60,'结果已核验')}
PHASES={'恢复正式服务':(40,'恢复正式服务'),'核验恢复':(45,'核验恢复'),'收集工作站预测':(50,'收集与校验结果')}


def advance(store,job,rank,label):
    now=time.time()
    with store.connect() as c:
        row=c.execute("SELECT state FROM jobs WHERE id=? AND owner=?",(job['id'],job['owner'])).fetchone()
        if not row or row['state']!='RUNNING':return
        c.execute('''INSERT INTO task_progress(job,rank,label,started,observed) VALUES(?,?,?,?,?)
          ON CONFLICT(job) DO UPDATE SET rank=excluded.rank,label=excluded.label,observed=excluded.observed
          WHERE excluded.rank>=task_progress.rank''',(job['id'],rank,label,now,now))


def terminal_text(job):
    from .wording import display_text
    return display_text(_terminal_text(job))


def _terminal_text(job):
    result=job.get('result') or {};state=job['state'];jid=job['id']
    if state=='SIMULATED':return '任务 '+jid+'：模拟验证完成，未操作台架。'
    if state in ('SUCCEEDED','POSTPROCESSING','PARTIAL_SUCCESS'):
        title='回灌结果已交付' if job['payload'].get('action')=='replay' else '评测完成'
        if job['payload'].get('action')=='replay' and result.get('report'):title='回灌与评测完成'
        text=title+'。任务：'+jid+'\n工作站结果目录：'+str(result.get('task',''))
        if state=='POSTPROCESSING':text+='\n评测已进入独立队列，完成后另行交付 TXT。'
        if result.get('report'):text+='\n原始 TXT：'+str(result['report'])
        if state=='PARTIAL_SUCCESS':text+='\n评测存在缺项或未完成，请核对评测状态与原始 TXT；回灌结果保留。'
        if result.get('assembly_pending'):text+='\n软件装配正在后台归档，不影响结果使用或下一任务。'
        return text
    if state=='CANCELLED':return '任务 '+jid+'：已取消尚未执行的任务。'
    diagnostic=result.get('diagnostics') or {}
    text='任务 '+jid+'：'+('结果待核验，未自动重跑。' if state=='NEEDS_REVIEW' else '已停止后续执行。')
    text+='\n'+(str(diagnostic.get('summary')) if diagnostic.get('summary') else '请核对本次材料或由管理员检查执行收据。')
    if diagnostic.get('next_action'):text+='\n下一步：'+str(diagnostic['next_action'])[:350]
    text+='\n已收材料和已有结果保留，无需重复提交整套材料。'
    return text
