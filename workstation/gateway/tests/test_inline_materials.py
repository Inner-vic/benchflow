import base64,copy,hashlib,hmac,html,io,json,os,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_confirmation_flow as fixtures
from bench_gateway import card_ui,input_cards,confirmations,inline_materials,card_entities,menu_events
from bench_gateway.core import GatewayError
from bench_gateway.task_cards import render


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values(): yield from nodes(item)
    elif isinstance(value, list):
        for item in value: yield from nodes(item)


class NativeInlineTests(unittest.TestCase):
    def setUp(self):
        fixtures.ConfirmationFlowTests.setUp(self)
        self.cfg.update(inline_material_card=True,native_material_menu=True,default_dataset_path='/datasets/shared/v151',m100_build_enabled=True)
        self.svc.cfg=self.cfg
        self.store.default_dataset_path=self.cfg['default_dataset_path']
        self.store.request_scoped_supplements=True

    def snapshot(self):
        task=self.tasks.current(self.user)
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(task['id'],)).fetchone())
        return input_cards.snapshot(self.cfg,self.store,row)

    def event(self,**changes):
        v=self.snapshot();ui=v['card_ui'];form={k:v for k,v in ui['form_values'].items() if k in ('app_source','build_reference','dataset','goal')};form.update(changes)
        return {'event_id':'inline-'+str(time.time_ns()),'message_id':'om_flow_card','action':'confirm_materials',
                'value':{'nonce':ui['nonce'],'revision':ui['form_revision']},'form_value':form}

    def test_render_has_visible_inputs_paths_and_one_confirmation(self):
        v=self.snapshot();card=render(v);text=json.dumps(card,ensure_ascii=False)
        form=card['body']['elements'][0]
        self.assertEqual('form',form['tag']);self.assertEqual('bench_form',form['element_id'])
        self.assertIn('/datasets/shared/v151',text)
        for forbidden in ('共享18场景','已登记软件包来源','调整选项','保存修改'):self.assertNotIn(forbidden,text)
        self.assertEqual(1,text.count('确认执行'));self.assertIn('libxlidar.so',text)
        fields = {x['name']:x for x in nodes(form) if x.get('tag') == 'input'}
        for name in ('app_source','build_reference','dataset'):
            self.assertEqual('text',fields[name]['input_type'])
            self.assertEqual('top',fields[name]['label_position'])
        self.assertEqual(['input']*3,[r['tag'] for r in form['elements'][:3]])
        self.assertNotIn('构建链接（选填）',text)
        self.assertEqual({'app_source','build_reference','dataset'},set(fields))
        self.assertIsNone(card_entities.find(card,'bench_optional'))
        libraries=html.unescape(' '.join(n['content'] for n in nodes(card_entities.find(card,'bench_files')['columns'][0]['elements'][0]) if n.get('tag')=='markdown'))
        for name in ('libxcamera.so','libxlidar.so','liburoad_m100.so'):self.assertIn(name,libraries)
        self.assertNotIn('\n',libraries)
        footer=form['elements'][-1]
        self.assertEqual('stretch',footer['flex_mode'])
        self.assertEqual(['replay_mode','goal'],[n['name'] for n in nodes(footer) if n.get('tag')=='select_static'])
        button=next(n for n in nodes(footer) if n.get('tag')=='button')
        self.assertEqual('submit',button['form_action_type'])
        self.assertEqual('bench:confirm_materials',button['behaviors'][0]['value']['action'])
        self.assertEqual(v['card_ui']['form_revision'],button['behaviors'][0]['value']['revision'])

    def test_resurfacing_replaces_old_native_form_with_readonly_card(self):
        class Client:
            def __init__(self):self.calls=[];self.n=0
            def auth(self,**kwargs):pass
            def _json(self,path,**kwargs):
                self.calls.append((path,kwargs))
                if path=='/cardkit/v1/cards':self.n+=1;return {'data':{'card_id':str(self.n)}}
                if path.startswith('/im/v1/messages?'):return {'data':{'message_id':'om_inline_'+str(self.n)}}
                return {'data':{}}
        client=Client()
        with self.store.connect() as c:c.execute("UPDATE input_cards SET message_id=NULL,digest='',last_sent=0 WHERE task_id=?",(self.task['id'],))
        with patch('bench_gateway.task_cards._client',return_value=client):
            self.assertTrue(input_cards.present(self.cfg,self.store,self.user,self.session))
            self.assertTrue(input_cards.present(self.cfg,self.store,self.user,self.session,surface=True))
        retired=[json.loads(req['body'])['card']['data'] for path,req in client.calls if path=='/cardkit/v1/cards/1' and req.get('method')=='PUT']
        self.assertEqual(1,len(retired));self.assertIsNone(card_entities.find(json.loads(retired[0]),'bench_form'))
        self.assertIn('此卡片已停用',retired[0])
        with self.store.connect() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_view_and_complete_materials_do_not_start_preparation(self):
        self.snapshot();self.flow.tick()
        with self.store.connect() as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM preparations').fetchone()[0])
            self.assertEqual(0,c.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_submit_current_form_applies_and_confirms_same_revision(self):
        self.intake.update(self.user,{'scene_scope':'2'})
        event=self.event(goal='replay_and_evaluate')
        text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('已确认',text);task=self.tasks.current(self.user)
        self.assertEqual('2',task['data']['scene_scope']);self.assertEqual('replay_and_evaluate',task['data']['goal'])
        with self.store.connect() as c:self.assertIsNotNone(confirmations.active(c,self.user,task,self.session))
        again=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('已处理',again)
        with self.store.connect() as c:self.assertEqual(1,c.execute("SELECT count(*) FROM material_confirmations WHERE state='ACTIVE'").fetchone()[0])

    def test_confirm_fallback_does_not_repeat_material_summary_or_ask_to_confirm_again(self):
        from bench_gateway import cc_agent
        event=self.event(goal='replay_and_evaluate');out=io.StringIO()
        with patch('bench_gateway.cc_agent.load',return_value=self.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':self.session}),patch('sys.stdin',io.StringIO(card_ui.PREFIX+'fixture')),patch('sys.stdout',out),patch('bench_gateway.card_ui.decode',return_value=event),patch('bench_gateway.card_ui.present',return_value=False):
            cc_agent.main()
        answer=next(json.loads(s)['item']['text'] for s in out.getvalue().splitlines() if json.loads(s)['type']=='item.completed')
        self.assertEqual('已确认本次材料，开始检查；通过后进入执行队列。',answer)
        with self.store.connect() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_delivered_confirm_ack_survives_phase_digest_change_but_not_a_failed_send(self):
        card_ui.handle(self.svc,self.user,self.session,self.event());result=self.svc.card_callback_result
        task=self.tasks.current(self.user);card_entities.schema(self.store)
        with self.store.connect() as c:
            c.execute("UPDATE input_cards SET state='CHECKING',revision=?,last_sent=?,attempts=0 WHERE task_id=?",(task['revision'],time.time()+1,task['id']))
            c.execute("INSERT INTO card_entities(send_key,card_id,message_id,card_json) VALUES('fixture','cid','om_flow_card',?)",(json.dumps({'schema':'2.0','body':{'elements':[{'tag':'markdown','content':'检查材料'}]}}),))
        with patch('bench_gateway.input_cards.present',return_value=False):
            self.assertTrue(card_ui.present(self.cfg,self.store,self.user,self.session,result))
            with self.store.connect() as c:c.execute('UPDATE input_cards SET attempts=1')
            self.assertFalse(card_ui.present(self.cfg,self.store,self.user,self.session,result))
            with self.store.connect() as c:
                c.execute('UPDATE input_cards SET attempts=0')
                c.execute('UPDATE card_entities SET card_json=?',(json.dumps({'tag':'form','element_id':'bench_form'}),))
            self.assertFalse(card_ui.present(self.cfg,self.store,self.user,self.session,result))

    def test_new_sensor_since_form_open_is_not_overwritten(self):
        event=self.event(goal='replay_and_evaluate')
        self.intake.update(self.user,{'camera_so':'C2'});self.flow.arm(self.user,self.session)
        text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('已确认',text);task=self.tasks.current(self.user)
        self.assertEqual('C2',task['data']['camera_so'])
        with self.store.connect() as c:self.assertEqual('C2',json.loads(confirmations.active(c,self.user,task,self.session)['snapshot'])['camera_so'])

    def test_conflicting_chat_and_form_field_does_not_execute(self):
        event=self.event(dataset='/datasets/form')
        self.intake.update(self.user,{'dataset':'/datasets/chat'});self.flow.arm(self.user,self.session)
        text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('同时修改',text);task=self.tasks.current(self.user)
        self.assertEqual('/datasets/chat',task['data']['dataset'])
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,task))

    def test_changed_app_can_create_next_material_and_confirm_without_old_binding(self):
        event=self.event(app_source='https://example.test/new.app.tar.gz',goal='replay_and_evaluate')
        text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('已确认',text);task=self.tasks.current(self.user)
        self.assertNotEqual(self.task['id'],task['id']);self.assertEqual('https://example.test/new.app.tar.gz',task['data']['package'])
        self.assertNotIn('m100_so',task['data']);self.assertEqual('build_if_missing',task['data']['m100_mode'])
        with self.store.connect() as c:self.assertIsNotNone(confirmations.active(c,self.user,task,self.session))

    def test_missing_sensor_after_submit_remains_unconfirmed(self):
        event=self.event(goal='replay_and_evaluate')
        with self.store.connect() as c:
            task=self.tasks.current(self.user);data=dict(task['data']);data.pop('lidar_so')
            c.execute('UPDATE task_requests SET data=? WHERE id=?',(json.dumps(data),task['id']))
            c.execute('UPDATE material_drafts SET data=? WHERE owner=?',(json.dumps(data),self.user))
        text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('尚缺材料',text);task=self.tasks.current(self.user)
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,task))

    def test_inflight_other_input_prevents_confirmation(self):
        from bench_gateway.incremental import InputBatch
        event=self.event(goal='replay_and_evaluate');batch=InputBatch(self.store,self.user)
        text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('仍在接收',text);batch.finish(True)
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,self.tasks.current(self.user)))

    def test_expected_revision_rejects_race_in_intake(self):
        current=self.tasks.current(self.user)
        self.intake.update(self.user,{'lidar_so':'L2'})
        with self.assertRaisesRegex(GatewayError,'确认期间'):
            self.svc.update_inputs(self.user,{'scene_scope':'9'},expected_task={'id':current['id'],'revision':current['revision']})
        self.assertEqual('L2',self.tasks.current(self.user)['data']['lidar_so'])

    def test_empty_app_is_not_displayed_as_registered(self):
        self.intake.update(self.user,{},new=True);self.flow.arm(self.user,self.session)
        v=self.snapshot();card=render(v);raw=json.dumps(card,ensure_ascii=False)
        self.assertFalse(v['material_display']['software_provided']);self.assertNotIn('已登记',raw)
        app=next(x for x in nodes(card) if x.get('name')=='app_source')
        self.assertEqual('',app['default_value']);self.assertTrue(app['required'])
        self.assertEqual('/datasets/shared/v151',self.tasks.current(self.user)['data']['dataset'])

    def test_legacy_confirm_button_cannot_bypass_inline_form(self):
        event=self.event();event['action']='confirm_execution'
        self.assertIn('已升级',card_ui.handle(self.svc,self.user,self.session,event))
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,self.tasks.current(self.user)))

    def test_missing_form_values_cannot_confirm_only_the_server_snapshot(self):
        event=self.event();event['form_value']={}
        self.assertIn('未完整提交',card_ui.handle(self.svc,self.user,self.session,event))
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,self.tasks.current(self.user)))

    def test_chat_supplements_update_receipt_without_becoming_form_inputs(self):
        event=self.event()
        self.intake.update(self.user,{'scene_scope':'2,3','yaml_request':'common.yaml debug.enable=true'})
        self.flow.arm(self.user,self.session)
        card=render(self.snapshot());receipt=card_entities.find(card,'bench_sources')['content']
        self.assertIn('仅本次有效',receipt);self.assertIn('2,3',receipt);self.assertIn('debug.enable=true',receipt)
        self.assertNotIn('scene_scope',{n.get('name') for n in nodes(card) if n.get('tag')=='input'})
        self.assertIn('已确认',card_ui.handle(self.svc,self.user,self.session,event))
        task=self.tasks.current(self.user)
        with self.store.connect() as c:frozen=json.loads(confirmations.active(c,self.user,task,self.session)['snapshot'])
        self.assertEqual('2,3',frozen['scene_scope']);self.assertIn('debug.enable=true',frozen['yaml_request'])

    def test_no_empty_supplement_section_and_no_old_form_submission(self):
        self.intake.update(self.user,{'scene_scope':'全部','yaml_request':'保留原配置'})
        card=render(self.snapshot())
        self.assertNotIn('补充信息',json.dumps(card,ensure_ascii=False))
        event=self.event(scene_scope='2')
        self.assertIn('通过聊天设置',card_ui.handle(self.svc,self.user,self.session,event))
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,self.tasks.current(self.user)))

    def test_form_length_is_checked_without_relying_on_client_counter(self):
        event=self.event(app_source='https://example.test/'+('a'*1001))
        self.assertIn('超过1000字符',card_ui.handle(self.svc,self.user,self.session,event))
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,self.tasks.current(self.user)))

    def test_partial_updates_only_touch_receipts_not_unsaved_inputs(self):
        class Client:
            def __init__(self):self.calls=[]
            def _json(self,path,**kwargs):
                self.calls.append((path,kwargs));return {'data':{'card_id':'123456','message_id':'om_entity'}}
        client=Client();first=render(self.snapshot())
        mid=card_entities.send(self.store,client,'oc_test',first,'send-once',lambda:4)
        second=copy.deepcopy(first)
        next(n for n in nodes(card_entities.find(second,'bench_files')) if n.get('tag')=='markdown')['content']='附件已更新'
        # An accidental new default must not replace the user's local input.
        next(x for x in nodes(second) if x.get('name')=='app_source')['default_value']='https://example.test/new-default.app.tar.gz'
        card_entities.update(self.store,client,mid,second,lambda:4)
        path,request=client.calls[-1];self.assertTrue(path.endswith('/batch_update'))
        actions=json.loads(json.loads(request['body'])['actions'])
        self.assertEqual(['bench_files'],[a['params']['element_id'] for a in actions])
        with self.store.connect() as c:cached=json.loads(c.execute('SELECT card_json FROM card_entities WHERE message_id=?',(mid,)).fetchone()[0])
        self.assertEqual(next(x for x in nodes(first) if x.get('name')=='app_source')['default_value'],
                         next(x for x in nodes(cached) if x.get('name')=='app_source')['default_value'])

    def test_menu_entity_resolves_p2p_once_and_uses_cardkit_full_update(self):
        class Client:
            def __init__(self):self.calls=[]
            def _json(self,path,**kwargs):
                self.calls.append((path,kwargs));return {'data':{'card_id':'123456','message_id':'om_new','chat_id':'oc_native'}}
        client=Client();loading={'schema':'2.0','body':{'elements':[{'tag':'markdown','content':'正在打开材料卡'}]}}
        self.assertEqual(('om_new','oc_native'),card_entities.send_owner(self.store,client,self.user,loading,'menu-stable',lambda:4))
        self.assertEqual(('om_new','oc_native'),card_entities.send_owner(self.store,client,self.user,loading,'menu-stable',lambda:4))
        self.assertEqual(2,len(client.calls))
        card_entities.update(self.store,client,'om_new',render(self.snapshot()),lambda:4)
        self.assertEqual('/cardkit/v1/cards/123456',client.calls[-1][0]);self.assertEqual('PUT',client.calls[-1][1]['method'])
        self.assertFalse(any(path.startswith('/im/') and req.get('method')=='PATCH' for path,req in client.calls))

    def test_path_resolution_is_not_ambiguous_scene_count_label(self):
        first={'id':'ds1','kind':'dataset','owner':'shared','name':'18场景','version':1,'data':{'remote_path':'/datasets/a'}}
        second={**first,'id':'ds2','name':'另一个18场景','data':{'remote_path':'/datasets/b'}}
        with patch.object(self.svc,'assets',return_value=[first,second]):
            self.assertEqual('ds2',self.svc.asset(self.user,'dataset','/datasets/b')['id'])
            with self.assertRaisesRegex(GatewayError,'尚未接入'):self.svc.asset(self.user,'dataset','/datasets/unknown')

    def test_menu_event_reuses_the_native_loading_message_and_is_idempotent(self):
        directory=Path(self.cfg['project_root'])/'runtime';directory.mkdir()
        (directory/'credentials.json').write_text(json.dumps({'app_id':'cli_test','app_secret':'test-only-secret'}))
        source_session='feishu:'+self.user+':'+self.user
        raw=json.dumps({'schema':1,'event_id':'menu-once','app_id':'cli_test','operator_id':self.user,'chat_id':self.user,
                        'action':'open_materials','value':{'menu_version':2}}).encode()
        signature=hmac.new(b'test-only-secret',raw,hashlib.sha256).hexdigest()
        packet=menu_events.PREFIX+base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+signature
        before=self.tasks.current(self.user)
        class Client:
            def __init__(self):self.calls=[]
            def _json(self,path,**kwargs):
                self.calls.append((path,kwargs))
                return {'data':{'card_id':'123456','message_id':'om_menu','chat_id':'oc_test'}}
        client=Client()
        with patch('bench_gateway.input_cards.present',return_value=True),patch('bench_gateway.task_cards._client',return_value=client):
            self.assertEqual('NO_REPLY',menu_events.handle(self.svc,source_session,packet))
            self.assertEqual('NO_REPLY',menu_events.handle(self.svc,source_session,packet))
        self.assertEqual(1,sum(path.startswith('/im/v1/messages?') for path,_ in client.calls))
        self.assertEqual(before,self.tasks.current(self.user))
        with self.store.connect() as c:
            self.assertEqual('om_menu',c.execute('SELECT message_id FROM input_cards WHERE task_id=?',(before['id'],)).fetchone()[0])
            self.assertEqual(0,c.execute('SELECT count(*) FROM jobs').fetchone()[0])
        with self.assertRaisesRegex(GatewayError,'来源'):
            menu_events.handle(self.svc,source_session,packet[:-1]+'z')


if __name__=='__main__':unittest.main()
