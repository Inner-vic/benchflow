import html,json,time,unittest
import test_confirmation_flow as fixtures
from bench_gateway import card_ui,input_cards
from bench_gateway.parser import quick
from bench_gateway.task_cards import render

class RoutineUXTests(unittest.TestCase):
    event=fixtures.ConfirmationFlowTests.event
    def setUp(self):fixtures.ConfirmationFlowTests.setUp(self)
    def row(self):
        with self.store.connect() as c:return dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(self.task['id'],)).fetchone())
    def test_ordinary_start_requests_open_materials_without_model_or_execution(self):
        for message in ('帮我跑一轮台架评测','请帮我执行一次台架评测','我想做一次数据回灌','能帮我进行一下台架评测吗？','我要做一次台架评测','开始数据回灌','当前配置','查看当前配置'):
            with self.subTest(message=message):self.assertEqual({'action':'open_materials'},quick(message))
        for message in ('台架评测是什么？','如果开始数据回灌会怎样？','不要开始台架评测','数据回灌结果目录在哪'):
            with self.subTest(message=message):self.assertNotEqual('open_materials',(quick(message) or {}).get('action'))
    def test_switched_material_card_closes_old_editor_and_actions(self):
        event=self.event();event['action']='open_edit';card_ui.handle(self.svc,self.user,self.session,event)
        self.intake.update(self.user,{'package':'https://example.test/new.app.tar.gz'},new=True);self.flow.arm(self.user,self.session)
        v=input_cards.snapshot(self.cfg,self.store,self.row());card=render(v);text=html.unescape(json.dumps(card,ensure_ascii=False))
        self.assertEqual('CANCELLED',v['state']);self.assertFalse(v['confirm_ready'])
        self.assertNotIn('"tag": "form"',text);self.assertNotIn('"tag": "button"',text)
        self.assertNotIn('点击“确认执行”',text)
    def test_old_card_cannot_restore_or_overwrite_current_materials(self):
        event=self.event();event['action']='open_edit';card_ui.handle(self.svc,self.user,self.session,event)
        self.intake.update(self.user,{'package':'https://example.test/new.app.tar.gz'},new=True);before=self.tasks.current(self.user)
        for action in ('open_edit','save_material','cancel_edit','confirm_execution'):
            e=self.event();e.update(action=action,form_value={'scene_scope':'9'} if action=='save_material' else {})
            with self.subTest(action=action):
                self.assertIn('材料已切换',card_ui.handle(self.svc,self.user,self.session,e))
                self.assertEqual(before,self.tasks.current(self.user))
    def seed_finished_task(self,state):
        with self.store.connect() as c:
            c.execute('INSERT INTO jobs(id,owner,session,request_key,state,payload,result,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                      ('prior-run',self.user,self.session,'prior',state,json.dumps({'action':'replay','assets':{}}),'{}',time.time(),time.time()))
            c.execute('UPDATE task_requests SET job_id=?,state=? WHERE id=?',('prior-run',state,self.task['id']))
            c.execute("UPDATE automatic_tasks SET state='SUBMITTED',job_id='prior-run' WHERE task_id=?",(self.task['id'],))
    def test_menu_after_success_allows_confirmed_repeat_but_viewing_never_runs(self):
        self.seed_finished_task('SUCCEEDED')
        self.svc.handle(self.session,'当前配置',quick('当前配置'))
        current=self.tasks.current(self.user)
        self.assertNotEqual(self.task['id'],current['id']);self.assertIsNone(current['job_id'])
        self.assertEqual('A',current['data']['package'])
        with self.store.connect() as c:
            lineage=c.execute('SELECT * FROM task_lineage WHERE task_id=?',(current['id'],)).fetchone()
            self.assertEqual(1,lineage['force_run']);self.assertEqual('prior-run',lineage['base_job_id'])
            self.assertEqual(1,c.execute('SELECT count(*) FROM jobs').fetchone()[0])
        self.flow.tick()
        with self.store.connect() as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM preparations').fetchone()[0])
            self.assertEqual('AWAITING_CONFIRMATION',c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(current['id'],)).fetchone()[0])
        self.svc.handle(self.session,'当前配置',quick('当前配置'))
        self.assertEqual(current,self.tasks.current(self.user))
    def test_menu_after_failed_run_does_not_authorize_identical_failure_retry(self):
        self.seed_finished_task('FAILED')
        self.svc.handle(self.session,'当前配置',quick('当前配置'))
        current=self.tasks.current(self.user)
        with self.store.connect() as c:
            lineage=c.execute('SELECT force_run FROM task_lineage WHERE task_id=?',(current['id'],)).fetchone()
            self.assertEqual(0,lineage[0]);self.assertEqual(1,c.execute('SELECT count(*) FROM jobs').fetchone()[0])
    def test_checking_shows_elapsed_but_idle_cards_do_not_count_up(self):
        card_ui.handle(self.svc,self.user,self.session,self.event())
        with self.store.connect() as c:c.execute("UPDATE automatic_tasks SET state='CHECKING' WHERE task_id=?",(self.task['id'],))
        v=input_cards.snapshot(self.cfg,self.store,self.row());v['elapsed']='2分30秒';text=html.unescape(json.dumps(render(v),ensure_ascii=False))
        self.assertEqual('CHECKING',v['state']);self.assertIn('检查用时 2分30秒',text)
        v.update(state='AWAITING_CONFIRMATION',label='请确认本次材料')
        self.assertNotIn('检查用时',html.unescape(json.dumps(render(v),ensure_ascii=False)))

if __name__=='__main__':unittest.main()
