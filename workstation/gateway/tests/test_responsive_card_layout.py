"""Native responsive capabilities and content integrity, not device screenshots."""
import copy,html,json,unittest
import test_inline_materials as fixtures
from bench_gateway.card_layout import materials,details,shell
from bench_gateway.task_cards import render,scene_progress,workflow_columns


class ResponsiveCardTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.NativeInlineTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.value=self.f.snapshot()

    def test_inputs_have_full_width_and_do_not_share_phone_space_with_side_labels(self):
        card=render(self.value)
        inputs=[n for n in fixtures.nodes(card) if n.get('tag')=='input']
        self.assertEqual(3,len(inputs))
        for n in inputs:
            self.assertEqual('fill',n['width']);self.assertEqual('top',n['label_position'])
            self.assertEqual('text',n['input_type'])
            self.assertEqual(1000,n['max_length'])
        rows=card['body']['elements'][0]['elements'][:3]
        self.assertTrue(all(row['tag']=='input' for row in rows))
        files=next(n for n in fixtures.nodes(card) if n.get('element_id')=='bench_files')
        panel=files['columns'][0]['elements'][0]
        self.assertEqual('stretch',panel['flex_mode']);self.assertEqual(3,len(panel['columns']))

    def test_mode_goal_and_confirmation_wrap_as_whole_compact_controls(self):
        form=render(self.value)['body']['elements'][0];footer=form['elements'][-1]
        self.assertEqual('stretch',footer['flex_mode']);self.assertEqual(3,len(footer['columns']))
        self.assertTrue(all(c['width']=='weighted' and c['vertical_align']=='center' for c in footer['columns']))
        self.assertEqual([2,2,1],[c['weight'] for c in footer['columns']])
        for col in footer['columns'][:2]:
            row=col['elements'][0]
            self.assertEqual('none',row['flex_mode']);self.assertEqual(2,len(row['columns']))
            self.assertEqual('56px',row['columns'][0]['width'])
        self.assertEqual('right',footer['columns'][-1]['horizontal_align'])
        controls=[n for n in fixtures.nodes(footer) if n.get('tag') in ('select_static','button')]
        self.assertEqual(['replay_mode','goal','bench_confirm_'+self.value['card_ui']['nonce']+'_'+str(self.value['card_ui']['form_revision'])],[n['name'] for n in controls])
        self.assertEqual(['120px','120px','100px'],[n['width'] for n in controls])
        self.assertLessEqual(56+8+int(controls[0]['width'][:-2]),200)
        self.assertLessEqual(2*(56+8+120)+100+16,552)
        self.assertEqual('submit',controls[-1]['form_action_type'])
        self.assertEqual('bench:confirm_materials',controls[-1]['behaviors'][0]['value']['action'])

    def test_full_urls_and_paths_are_preserved_without_hidden_break_characters(self):
        v=copy.deepcopy(self.value)
        url='https://example.test/Acore/Civu/Release/app/CCU_LIVC_AVM-release-SW0058_ULTRA_ID0111.20260918-2737.app.tar.gz'
        path='/mnt/data/bench-eval/datasets/v151-20260918-001/DataInLoop/gt-analyze/v151'
        v['material_display'].update(software_url=url,dataset_path=path)
        v['task']='/mnt/data/bench-eval/tasks/long-result-directory-20261027-1234567890'
        text=html.unescape(json.dumps([materials(v),details(v)],ensure_ascii=False))
        for value in (url,path,v['task']):self.assertIn(value,text)
        self.assertNotIn('\u200b',text);self.assertNotIn('`'+v['task']+'`',text)
        self.assertEqual(1,len(materials(v)['columns']))

    def test_fonts_and_workflow_use_native_device_adaptation(self):
        card=render(self.value);style=card['config']['style']['text_size']
        self.assertEqual('normal',style['bench_caption']['mobile'])
        self.assertEqual('notation',style['bench_caption']['pc'])
        nodes=workflow_columns(self.value)
        self.assertEqual('flow',nodes['flex_mode']);self.assertEqual(4,len(nodes['columns']))

    def test_bar_stays_short_without_falsifying_counts_or_completion(self):
        v={**copy.deepcopy(self.value),'input_phase':False,'state':'RUNNING','phase_rank':30,
           'total':1000,'completed':999,'detail':'','tone':'blue'}
        text=scene_progress(v)
        self.assertIn('999/1000',text);self.assertNotIn('100%',text)
        self.assertEqual(12,text.count('▰')+text.count('▱'));self.assertEqual(1,text.count('▱'))


if __name__=='__main__':unittest.main()
