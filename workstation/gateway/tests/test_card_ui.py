import base64,copy,hashlib,hmac,json,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
from bench_gateway.core import Store,GatewayError
from bench_gateway.service import Service
from bench_gateway.tasks import Tasks
from bench_gateway.intake import Intake
from bench_gateway.incremental import intake_blocked
from bench_gateway import card_ui,input_cards
from bench_gateway.task_cards import snapshot,render

class CardUITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg={'project_root':self.tmp.name,'state_root':str(Path(self.tmp.name)/'state'),'mode':'simulate','live_enabled':False,
                  'auto_execute_on_complete':True,'unified_status_cards':True,'task_cards_enabled':True,'notifications_enabled':True,
                  'card_callbacks_enabled':True,'feishu_app_id':'cli_card_test','cc_project':'bench','workstation_evaluation_enabled':True}
        self.service=Service(self.cfg);self.store=self.service.store;self.user='ou_card_owner';self.other='ou_card_other';self.session='feishu:oc_card:'+self.user
        for u in (self.user,self.other):self.store.allow(u)
        self.tasks=Tasks(self.store);self.intake=Intake(self.store)
        self.intake.update(self.user,{'package':'A','camera_so':'C','lidar_so':'L','m100_so':'M','scene_scope':'1'})
        self.task=self.tasks.current(self.user);self.ui=card_ui.ensure(self.store,self.task)
        input_cards.ensure(self.cfg,self.store,self.user,self.session,self.task)
        with self.store.connect() as c:c.execute("UPDATE input_cards SET message_id='om_card_original' WHERE task_id=?",(self.task['id'],))
    def event(self,action,form=None,**values):
        return {'schema':1,'event_id':'e-'+str(time.time_ns()),'app_id':'cli_card_test','operator_id':self.user,'chat_id':'oc_card','message_id':'om_card_original',
                'action':action,'value':{'nonce':self.ui['nonce'],**values},'form_value':form or {}}
    def handle(self,action,form=None,**values):return card_ui.handle(self.service,self.user,self.session,self.event(action,form,**values))
    def freeze(self):
        p={'action':'replay','evaluate_after':True,'scenes':[1],'baseline':{'data':{'evaluator':{'argv':['test-reviewed']}}},'assets':{}}
        j,_=self.store.submit(self.user,self.session,'one',p);self.tasks.link(self.user,j);self.store.claim();return j
    def test_signed_form_bytes_and_operator_are_verified_before_dispatch(self):
        data=self.event('save_material',{'yaml_request':'打开开关'});raw=json.dumps(data,ensure_ascii=False).encode();secret=b'test-key'
        packet=card_ui.PREFIX+base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+hmac.new(secret,raw,hashlib.sha256).hexdigest()
        self.assertEqual(data,card_ui.decode(self.cfg,self.session,packet,secret=secret))
        with self.assertRaises(GatewayError):card_ui.decode(self.cfg,self.session,packet+'0',secret=secret)
        with self.assertRaises(GatewayError):card_ui.decode(self.cfg,'feishu:oc_other:'+self.user,packet,secret=secret)
    def test_open_form_blocks_auto_admission_until_save(self):
        self.handle('open_edit')
        with self.store.connect() as c:self.assertTrue(intake_blocked(c,self.user))
        self.handle('save_material',{'scene_scope':'2','goal':'replay'})
        with self.store.connect() as c:self.assertFalse(intake_blocked(c,self.user))
        draft=self.intake.get(self.user);self.assertEqual('A',draft['package']);self.assertEqual('C',draft['camera_so']);self.assertEqual('2',draft['scene_scope']);self.assertEqual('replay',draft['goal'])
    def test_form_has_real_submit_and_blank_inherits_values(self):
        self.handle('open_edit')
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(self.task['id'],)).fetchone())
        value=input_cards.snapshot(self.cfg,self.store,row);card=render(value);form=next(x for x in card['body']['elements'] if x['tag']=='form')
        self.assertTrue(any(x.get('form_action_type')=='submit' and 'behaviors' not in x for x in form['elements']))
        self.assertNotIn('file_upload',json.dumps(card));self.assertIn('聊天窗口附件',json.dumps(card,ensure_ascii=False))
        self.handle('save_material',{'app_source':'','dataset':'','scene_scope':'2','goal':'replay_and_evaluate'})
        self.assertEqual('A',self.intake.get(self.user)['package'])

    def test_unchanged_prefilled_url_and_dataset_alias_do_not_change_material_or_replay(self):
        self.tasks.update(self.user,self.task['id'],{'package':'a-own-package','dataset':'ds-own-data'})
        with patch('bench_gateway.material_summary.describe',return_value={'software_url':'https://example.test/2737.app.tar.gz','dataset':'共享:18场景'}):
            self.handle('open_edit')
        before=self.tasks.current(self.user)
        ui=card_ui.describe(self.cfg,self.store,before)
        fields={f.get('name'):f for f in card_ui.material_form(ui)['elements']}
        self.assertEqual('https://example.test/2737.app.tar.gz',fields['app_source']['default_value'])
        self.assertEqual('共享:18场景',fields['dataset']['default_value'])
        self.handle('save_material',ui['prefill'])
        self.assertEqual(before,self.tasks.current(self.user));self.assertEqual([],self.store.jobs(self.user))
        with self.store.connect() as c:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_tasks'").fetchone()
            row=c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(before['id'],)).fetchone() if exists else None
        self.assertTrue(row is None or row[0]=='AWAITING_MATERIAL','unchanged input must never arm execution')

    def test_goal_and_build_reference_only_form_never_arms_a_new_replay(self):
        self.handle('open_edit');self.handle('save_material',{'goal':'replay','build_reference':'https://ci.example.test/build/2737'})
        current=self.tasks.current(self.user);self.assertEqual('replay',current['data']['goal'])
        self.assertEqual('https://ci.example.test/build/2737',current['data']['build_reference'])
        with self.store.connect() as c:self.assertEqual('AWAITING_MATERIAL',c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(current['id'],)).fetchone()[0])

    def test_materials_show_real_owned_filename_and_distinguish_waiting_replacement(self):
        with self.store.connect() as c:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?)',('a-lidar',self.user,'dependency','lidar',1,json.dumps({'filename':'xlidar.so'}),time.time()))
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?)',('a-foreign',self.other,'dependency','private',1,json.dumps({'filename':'private-camera.so'}),time.time()))
            c.execute('INSERT INTO card_expected_uploads VALUES(?,?,?,?)',(self.task['id'],self.user,'lidar',time.time()))
        self.tasks.update(self.user,self.task['id'],{'lidar_so':'a-lidar','camera_so':'a-foreign'})
        value=card_ui.describe(self.cfg,self.store,self.tasks.current(self.user))
        self.assertEqual('xlidar.so；等待替换附件',value['materials']['雷达 SO'])
        self.assertNotIn('private-camera.so',json.dumps(value))
    def test_promised_file_keeps_task_waiting_until_associated(self):
        self.handle('open_edit');self.handle('save_material',{'scene_scope':'2','pending_uploads':['camera']})
        with self.store.connect() as c:
            self.assertTrue(intake_blocked(c,self.user))
            c.execute('''CREATE TABLE task_attachments(id TEXT,owner TEXT,task_id TEXT,filename TEXT,state TEXT,data TEXT,created REAL)''')
            c.execute('INSERT INTO task_attachments VALUES(?,?,?,?,?,?,?)',('att-1',self.user,self.task['id'],'libxcamera.so','RECEIVED',json.dumps({'role':'camera'}),time.time()))
        with self.store.connect() as c:self.assertTrue(intake_blocked(c,self.user))
        with self.store.connect() as c:c.execute("UPDATE task_attachments SET state='ASSOCIATED'")
        with self.store.connect() as c:self.assertFalse(intake_blocked(c,self.user))
    def test_actual_goal_button_updates_same_run_and_feedback(self):
        j=self.freeze();original=copy.deepcopy(j['payload']);self.handle('set_goal',goal='replay')
        saved=self.store.job(self.user,j['id']);self.assertEqual(original,saved['payload']);self.assertEqual(1,len(self.store.jobs(self.user)))
        value=snapshot(self.store,saved,cfg=self.cfg);raw=json.dumps(render(value),ensure_ascii=False)
        self.assertFalse(value['evaluate_after']);self.assertIn('已生效',raw)
    def test_dispatched_evaluation_refusal_is_visible_on_original_card(self):
        j=self.freeze();self.store.complete_replay(self.store.job(self.user,j['id']),{'state':'PASS','task':'/data/result'})
        self.store.claim('postprocess');self.handle('set_goal',goal='replay')
        raw=json.dumps(render(snapshot(self.store,self.store.job(self.user,j['id']),cfg=self.cfg)),ensure_ascii=False)
        self.assertIn('未生效',raw);self.assertIn('已派发',raw)
    def test_replayed_callback_does_not_repeat_action(self):
        self.freeze();e=self.event('set_goal',goal='replay');card_ui.handle(self.service,self.user,self.session,e)
        self.handle('set_goal',goal='replay_and_evaluate')
        text=card_ui.handle(self.service,self.user,self.session,e)
        self.assertIn('未重复执行',text)
        with self.store.connect() as c:self.assertEqual(1,c.execute('SELECT evaluate_after FROM execution_controls').fetchone()[0])
    def test_foreign_card_or_replaced_message_does_not_mutate(self):
        before=self.tasks.current(self.user);e=self.event('open_edit');e['message_id']='om_wrong'
        self.assertIn('未生效',card_ui.handle(self.service,self.user,self.session,e));self.assertEqual(before,self.tasks.current(self.user))
        self.assertIn('未生效',card_ui.handle(self.service,self.other,'feishu:oc_other:'+self.other,self.event('open_edit')))
    def test_callback_cannot_move_an_original_private_card_to_another_chat(self):
        before=self.tasks.current(self.user)
        result=card_ui.handle(self.service,self.user,'feishu:oc_group:'+self.user,self.event('open_edit'))
        self.assertIn('本人私聊',result);self.assertEqual(before,self.tasks.current(self.user))
    def test_editing_submitted_material_prepares_a_new_held_draft(self):
        old=self.freeze();before=self.tasks.current(self.user);self.handle('open_edit')
        new=self.tasks.current(self.user);self.assertNotEqual(before['id'],new['id']);self.assertIsNone(new['job_id']);self.assertTrue(self.service.defer_auto)
        self.assertEqual(old['payload'],self.store.job(self.user,old['id'])['payload'])
        with self.store.connect() as c:self.assertTrue(intake_blocked(c,self.user))
    def test_unknown_form_fields_fail_without_modifying_material(self):
        self.handle('open_edit');before=self.tasks.current(self.user)
        text=self.handle('save_material',{'command':'run me'})
        self.assertIn('未生效',text);self.assertEqual(before,self.tasks.current(self.user))
    def test_edit_another_material_never_blocks_unrelated_selected_task(self):
        self.handle('open_edit');self.intake.update(self.user,{'package':'B'},new=True)
        with self.store.connect() as c:self.assertFalse(card_ui.blocks(c,self.user))
    def test_new_release_form_preserves_explicit_goal_and_closes_old_editor(self):
        self.handle('open_edit');self.handle('save_material',{'app_source':'https://example.test/new.app.tar.gz','goal':'replay'})
        new=self.tasks.current(self.user);self.assertEqual('replay',new['data']['goal']);self.assertNotEqual(self.task['id'],new['id'])
        with self.store.connect() as c:self.assertEqual(0,c.execute('SELECT editing FROM card_ui WHERE task_id=?',(self.task['id'],)).fetchone()[0])
    def test_files_received_during_edit_resume_when_form_has_no_text_changes(self):
        self.handle('open_edit');self.intake.update(self.user,{'camera_so':'C2'})
        self.handle('save_material',{'goal':'replay_and_evaluate'})
        with self.store.connect() as c:
            self.assertFalse(intake_blocked(c,self.user))
            self.assertEqual('WAITING',c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(self.task['id'],)).fetchone()[0])
    def test_promised_upload_can_be_removed_and_existing_choice_survives_reopen(self):
        self.handle('open_edit');self.handle('save_material',{'scene_scope':'2','pending_uploads':['camera']})
        self.handle('open_edit');ui=card_ui.describe(self.cfg,self.store,self.tasks.current(self.user))
        form=card_ui.material_form(ui)
        select=next(x for x in form['elements'] if x.get('name')=='pending_uploads')
        self.assertEqual(['camera'],select['selected_values'])
        self.handle('save_material',{'scene_scope':'3','pending_uploads':[]})
        with self.store.connect() as c:self.assertFalse(intake_blocked(c,self.user))

    def test_pause_remains_visible_with_expected_files_and_failed_previous_intake(self):
        from bench_gateway.autoflow import AutoFlow
        from bench_gateway.incremental import InputBatch
        self.handle('open_edit');self.handle('save_material',{'pending_uploads':['camera']})
        InputBatch(self.store,self.user,'files:libxcamera.so').finish(False)
        AutoFlow(self.cfg,self.store).control(self.user,self.session,True)
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(self.task['id'],)).fetchone())
        value=input_cards.snapshot(self.cfg,self.store,row)
        self.assertEqual('PAUSED',value['state']);self.assertIn('暂停',value['label']);self.assertNotIn('齐备后自动检查',value['detail'])
    def test_repeated_next_edit_reuses_draft_and_preserves_unsaved_form(self):
        self.freeze();self.handle('open_edit');first=self.tasks.current(self.user)
        with self.store.connect() as c:before=dict(c.execute('SELECT * FROM card_ui WHERE task_id=?',(first['id'],)).fetchone())
        self.handle('open_edit');self.assertEqual(first['id'],self.tasks.current(self.user)['id'])
        with self.store.connect() as c:after=dict(c.execute('SELECT * FROM card_ui WHERE task_id=?',(first['id'],)).fetchone())
        self.assertEqual(before,after)
    def test_old_task_edit_never_replaces_another_release_draft(self):
        self.freeze();self.intake.update(self.user,{'package':'B'});new=self.tasks.current(self.user)
        text=self.handle('open_edit')
        self.assertIn('未覆盖',text);self.assertEqual(new,self.tasks.current(self.user))
