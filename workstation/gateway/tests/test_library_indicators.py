import json,unittest
import test_confirmation_flow as fixtures
from bench_gateway import card_materials,card_layout,card_ui
from bench_gateway.preparation import fingerprint

class LibraryIndicators(unittest.TestCase):
    setUp=fixtures.ConfirmationFlowTests.setUp

    def display(self,files=()):
        return card_materials.describe(self.store,self.user,task=self.tasks.current(self.user),files=files)

    def checked(self,issues,stale=False,foreign=False):
        card_ui.handle(self.svc,self.user,self.session,fixtures.ConfirmationFlowTests.event(self))
        self.flow.tick();task=self.tasks.current(self.user)
        with self.store.connect() as c:
            c.execute('UPDATE preparations SET result=?,fingerprint=?,owner=? WHERE id=?',
                      (json.dumps({'issues':issues}), 'old' if stale else fingerprint(task['data']), 'other' if foreign else self.user,task['prep_id']))

    def test_provided_is_checked_without_claiming_validation(self):
        row=self.display()['rows'][0];self.assertEqual(row['indicator'],'provided')
        text=card_layout.library_line([row]);self.assertIn('☑',text);self.assertIn('已提供',text);self.assertNotIn('验证通过',text)

    def test_missing_or_planned_compile_is_empty(self):
        task=self.tasks.current(self.user);task['data'].update(camera_so='',m100_so='',m100_mode='build_if_missing')
        rows=card_materials.describe(self.store,self.user,task=task)['rows']
        self.assertEqual(['missing','provided','missing'],[r['indicator'] for r in rows])
        self.assertIn('☐',card_layout.library_line([rows[2]]));self.assertEqual('系统编译',rows[2]['status'])

    def test_matching_current_error_marks_only_affected_library(self):
        self.checked([{'field':'camera_so','code':'PROVIDER_SYMBOLS_MISSING','message':'与本次M100不匹配'}])
        rows=self.display()['rows'];self.assertEqual(['error','provided','provided'],[r['indicator'] for r in rows])
        text=json.dumps(card_layout.library_panel(rows),ensure_ascii=False)
        self.assertIn('☒',text);self.assertIn('校验错误',text);self.assertIn('与本次M100不匹配',text)

    def test_stale_or_foreign_check_cannot_mark_current_files_wrong(self):
        self.checked([{'field':'camera_so','message':'旧检查错误'}],stale=True)
        self.assertTrue(all(r['indicator']=='provided' for r in self.display()['rows']))
        with self.store.connect() as c:
            task=self.tasks.current(self.user)
            c.execute('UPDATE preparations SET fingerprint=?,owner=? WHERE id=?',(fingerprint(task['data']),'another-user',task['prep_id']))
        self.assertTrue(all(r['indicator']=='provided' for r in self.display()['rows']))

    def test_receiving_empty_then_save_failure_error(self):
        item={'state':'RECEIVING','data':json.dumps({'target':'lib/libxcamera.so'})}
        self.assertEqual('missing',self.display([item])['rows'][0]['indicator'])
        item['state']='FAILED';row=self.display([item])['rows'][0]
        self.assertEqual('error',row['indicator']);self.assertIn('保存失败',card_layout.library_line([row]))

    def test_group_check_problem_is_explicit_and_markup_is_escaped(self):
        self.checked([{'field':'dependencies','message':'检查异常 <script> & [link](bad)'}])
        rows=self.display()['rows'];self.assertTrue(all(r['indicator']=='error' for r in rows))
        text=json.dumps(card_layout.library_panel(rows),ensure_ascii=False)
        self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text)
        for token in ('callback','checker','confirm_materials'):self.assertNotIn(token,text)

if __name__=='__main__':unittest.main(verbosity=2)
