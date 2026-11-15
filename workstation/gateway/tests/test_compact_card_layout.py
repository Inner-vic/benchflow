import copy,html,json,unittest
import test_confirmation_flow as fixtures
from bench_gateway import input_cards,card_materials
from bench_gateway.task_cards import render

class CompactLayoutTests(unittest.TestCase):
    def setUp(self):
        fixtures.ConfirmationFlowTests.setUp(self)
        self.intake.update(self.user,{'camera_so':'https://example.test/xcamera.so','lidar_so':'https://example.test/xlidar.so','m100_so':'','m100_mode':'build_if_missing','dataset':'/mnt/work/data/v151','yaml_request':'用工作站文件 /mnt/u/common_param.yaml 替换 APP 内 etc/uroad/config/common_param.yaml'})
        self.flow.arm(self.user,self.session);self.task=self.tasks.current(self.user)
        with self.store.connect() as c:self.row=dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(self.task['id'],)).fetchone())
        self.value=input_cards.snapshot(self.cfg,self.store,self.row)
    def text(self,card):return html.unescape(json.dumps(card,ensure_ascii=False))
    def test_main_uses_actual_names_status_and_full_workstation_path(self):
        card=render(self.value);main=self.text({**card,'body':{'elements':[x for x in card['body']['elements'] if x['tag']!='collapsible_panel']}})
        for text in ('LIVC APP','xcamera.so','xlidar.so','liburoad_m100.so','系统编译','已提供','common_param.yaml','/mnt/work/data/v151'):self.assertIn(text,main)
        for text in ('相机 SO','雷达 SO','自动准备','自行编译'):self.assertNotIn(text,main)
        self.assertEqual(4,len(card['body']['elements']))
    def test_provided_m100_is_distinct_from_system_compilation(self):
        self.intake.update(self.user,{'m100_so':'https://example.test/liburoad_m100.so','m100_mode':'provided'})
        d=card_materials.describe(self.store,self.user,task=self.tasks.current(self.user))
        self.assertEqual('已提供',d['rows'][2]['status']);self.assertNotEqual('系统编译',d['m100_method'])
    def test_same_material_panel_is_reused_at_confirmation_running_and_completion(self):
        initial=render(self.value);material=initial['body']['elements'][1]
        for state in ('QUEUED','RUNNING','SUCCEEDED','FAILED'):
            value={**copy.deepcopy(self.value),'input_phase':False,'state':state,'label':state,'completed':1,'total':1,'task':'/mnt/results/run' if state=='SUCCEEDED' else '', 'phase_rank':30}
            card=render(value)
            self.assertEqual(material,card['body']['elements'][2]);self.assertEqual('blue',card['header']['template'])
            self.assertLessEqual(len(card['body']['elements']),5)
    def test_terminal_card_hides_old_confirmation_and_keeps_result_directory(self):
        v={**copy.deepcopy(self.value),'input_phase':False,'state':'SUCCEEDED','label':'回灌完成','completed':1,'total':1,'task':'/mnt/results/run','phase_rank':60}
        v['card_ui']['feedback']='已确认本次材料，开始检查；通过后进入执行队列。';v['card_ui']['feedback_at']=9999999999
        card=render(v);text=self.text(card)
        self.assertNotIn('开始检查',text);self.assertIn('/mnt/results/run',text)
    def test_app_url_is_complete_in_main_area_and_build_link_is_collapsed(self):
        v=copy.deepcopy(self.value);url='https://example.test/CCU_SW0058.20260918-2737.app.tar.gz'
        v['material_display'].update(software_url=url,build_reference='https://ci.example.test/job/build/2737/')
        card=render(v);folded=card['body']['elements'][2]
        self.assertEqual('collapsible_panel',folded['tag']);self.assertFalse(folded['expanded'])
        self.assertIn(url,self.text(card['body']['elements'][1]));self.assertNotIn(url,self.text(folded))
        self.assertIn('https://ci.example.test/job/build/2737/',self.text(folded))
    def test_yaml_is_optional_and_not_shown_until_requested(self):
        from bench_gateway.intake import CLEAR_YAML
        from bench_gateway.business import missing_inputs
        self.intake.update(self.user,{'yaml_request':CLEAR_YAML});self.flow.arm(self.user,self.session)
        value=input_cards.snapshot(self.cfg,self.store,self.row);d=value['material_display']
        self.assertFalse(d['yaml_requested']);self.assertEqual([],d['configs']);self.assertEqual(3,len(d['rows']))
        main=self.text(render(value)['body']['elements'][1])
        self.assertNotIn('common_param.yaml',main);self.assertNotIn('配置变更',main)
        self.assertFalse(any(x['field']=='yaml_request' for x in missing_inputs(self.tasks.current(self.user)['data'])))
    def test_cancel_yaml_does_not_redisplay_old_attached_file(self):
        from bench_gateway.intake import CLEAR_YAML
        self.intake.update(self.user,{'yaml_request':CLEAR_YAML})
        old=[{'filename':'common_param.yaml','state':'ASSOCIATED','data':'{}'}]
        d=card_materials.describe(self.store,self.user,task=self.tasks.current(self.user),files=old)
        self.assertFalse(d['yaml_requested']);self.assertEqual([],d['configs'])
    def test_new_app_does_not_require_old_optional_yaml(self):
        from bench_gateway.business import missing_inputs
        self.intake.update(self.user,{'package':'https://example.test/CCU_SW0075.20261023-2867.app.tar.gz'})
        self.flow.arm(self.user,self.session);task=self.tasks.current(self.user)
        d=card_materials.describe(self.store,self.user,task=task)
        self.assertFalse(task['data'].get('yaml_request'));self.assertFalse(d['yaml_requested']);self.assertEqual([],d['configs'])
        self.assertFalse(any(x['field']=='yaml_request' for x in missing_inputs(task['data'])))
    def test_natural_language_can_add_optional_yaml_later(self):
        from bench_gateway.intake import CLEAR_YAML
        from bench_gateway.parser import quick
        self.intake.update(self.user,{'yaml_request':CLEAR_YAML})
        message='把 common_param.yaml 的 log.concise 打开';intent=quick(message)
        self.assertEqual('prepare',intent['action']);self.svc.handle(self.session,message,intent)
        self.flow.arm(self.user,self.session);v=input_cards.snapshot(self.cfg,self.store,self.row)
        self.assertTrue(v['material_display']['yaml_requested']);self.assertIn('common_param.yaml',v['material_display']['configs'])
        self.assertIn('配置变更（可选）',self.text(render(v)['body']['elements'][1]))

if __name__=='__main__':unittest.main()
