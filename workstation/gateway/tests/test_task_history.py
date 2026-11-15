import json
import html
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from bench_gateway.core import Store
from bench_gateway.tasks import Tasks
from bench_gateway.intake import Intake
from bench_gateway.task_refs import reference
from bench_gateway.task_history import archive_history,restore_history,text,directories,task_name

class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.user='ou_history_owner';self.other='ou_history_other'
        for user in (self.user,self.other):self.store.allow(user)
        self.tasks=Tasks(self.store);self.intake=Intake(self.store);self.counter=0

    def create(self,name,state=None,user=None):
        user=user or self.user;self.counter+=1
        self.intake.update(user,{'package':name,'software_name':name,'goal':'replay'},new=True)
        task=self.tasks.current(user);job=None
        if state:
            job,_=self.store.submit(user,'feishu:oc_history:'+user,str(self.counter),{'action':'replay','scenes':[1],'evaluate_after':False})
            self.tasks.link(user,job)
            if state=='RUNNING':
                with self.store.connect() as c:c.execute("UPDATE jobs SET state='RUNNING' WHERE id=?",(job['id'],))
            elif state!='QUEUED':self.store.finish(job,state,{'task':'/mnt/data/bench-eval/tasks/'+job['id']})
            with self.store.connect() as c:c.execute('UPDATE outbox SET sent=1 WHERE job=?',(job['id'],))
        return task,job

    def test_archive_retains_current_material_assets_results_and_other_user(self):
        old,job=self.create('old-success','SUCCEEDED');draft,_=self.create('old-draft')
        current,_=self.create('current-material');other,_=self.create('other-private','SUCCEEDED',self.other)
        asset=self.store.add_asset(self.user,'package','retained',{'source':'kept'})
        before=self.store.job(self.user,job['id']);other_before=self.tasks.get(self.other,other['id'])
        result=archive_history(self.store,self.user)
        self.assertEqual(result['archived_records'],3);self.assertEqual(result['physical_files_deleted'],0)
        shown=self.tasks.text(self.user)
        self.assertIn('current-material',shown);self.assertNotIn('old-success',shown);self.assertNotIn('old-draft',shown)
        self.assertIn('old-success',text(self.store,self.user,history=True))
        self.assertEqual(self.tasks.current(self.user)['id'],current['id'])
        self.assertEqual(self.store.job(self.user,job['id']),before)
        self.assertEqual(self.tasks.get(self.other,other['id']),other_before)
        self.assertEqual(self.store.asset(self.user,'package','retained'),asset)

    def test_active_review_and_unfinished_delivery_are_never_archived(self):
        for state in ('QUEUED','RUNNING','NEEDS_REVIEW'):
            self.create('active-'+state,state)
        unsent,job=self.create('pending-delivery','SUCCEEDED')
        with self.store.connect() as c:c.execute('UPDATE outbox SET sent=0 WHERE job=?',(job['id'],))
        self.create('focus')
        result=archive_history(self.store,self.user)
        self.assertEqual(result['archived_records'],0)
        for name in ('active-QUEUED','active-RUNNING','active-NEEDS_REVIEW'):self.assertIn(name,html.unescape(self.tasks.text(self.user)))

    def test_restore_only_own_display_and_public_numbers_are_not_reused(self):
        old,_=self.create('old','SUCCEEDED');self.create('current')
        old_ref=reference(self.store,self.user,task_id=old['id']);archive_history(self.store,self.user)
        new,_=self.create('next');new_ref=reference(self.store,self.user,task_id=new['id'])
        self.assertNotEqual(old_ref,new_ref)
        self.assertEqual(restore_history(self.store,self.other),0)
        self.assertNotIn('old ·',self.tasks.text(self.user))
        self.assertGreater(restore_history(self.store,self.user),0)
        self.assertIn(old_ref,self.tasks.text(self.user))
        self.assertEqual(reference(self.store,self.user,task_id=old['id']),old_ref)

    def test_default_shows_current_active_and_only_three_recent_records(self):
        for i in range(8):self.create('finished-'+str(i),'SUCCEEDED')
        self.create('waiting','QUEUED');self.create('current')
        shown=self.tasks.text(self.user)
        self.assertIn('当前材料',shown);self.assertIn('执行与排队',shown);self.assertIn('最近记录',shown)
        self.assertEqual(shown.count('finished-'),3)
        self.assertEqual(text(self.store,self.user,history=True).count('finished-'),8)

    def test_independent_evaluation_and_directory_queries_remain_owner_scoped(self):
        task,job=self.create('source','SUCCEEDED');self.create('current')
        evaluation,_=self.store.submit(self.user,'chat','evaluation',{'action':'evaluate','source_job':job['id']})
        self.create('secret-other','SUCCEEDED',self.other)
        ref=reference(self.store,self.user,task_id=task['id'])
        self.assertIn(ref,self.tasks.text(self.user));self.assertIn('结果评测',self.tasks.text(self.user))
        self.assertNotIn('secret-other',self.tasks.text(self.user))
        archive_history(self.store,self.user)
        self.assertIn('source',self.tasks.text(self.user)) # active child protects its source
        mapping=directories(self.store,self.user)
        self.assertIn(ref,mapping);self.assertIn('/mnt/data/bench-eval/tasks/'+job['id'],mapping)
        self.assertNotIn('secret-other',mapping)

    def test_names_use_version_build_and_do_not_emit_mentions(self):
        name=task_name({'package':'https://artifactory.example/CCU_LIVC_AVM-release-SW0058_ULTRA_ID0111.20260918-2737.app.tar.gz','goal':'replay'})
        self.assertEqual(name,'SW0058 · 构建 2737 · 仅回灌')
        self.assertNotIn('<at',task_name({'software_name':'<at id=all>test'}))

    def test_archived_visibility_never_hides_a_later_review_state(self):
        task,job=self.create('restored-review','SUCCEEDED');self.create('current')
        archive_history(self.store,self.user)
        with self.store.connect() as c:c.execute("UPDATE jobs SET state='NEEDS_REVIEW' WHERE id=?",(job['id'],))
        self.assertIn('restored-review',self.tasks.text(self.user))
        self.assertIn('执行或恢复待核验',self.tasks.text(self.user))

    def test_selected_old_task_remains_visible_and_open_attachments_block_cleanup(self):
        old,_=self.create('selected-old','SUCCEEDED')
        for i in range(5):self.create('newer-'+str(i),'SUCCEEDED')
        self.tasks.select(self.user,old['id'])
        self.assertIn('selected-old',self.tasks.text(self.user))
        with self.store.connect() as c:c.execute("INSERT INTO input_batches VALUES('batch',?,'OPEN',1,1,'claim')",(self.user,))
        from bench_gateway.core import GatewayError
        with self.assertRaises(GatewayError):archive_history(self.store,self.user)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM task_history_archives').fetchone()[0],0)

    def test_asset_package_name_is_resolved_only_within_owner(self):
        own=self.store.add_asset(self.user,'package','same-name',{'filename':'CCU_LIVC_AVM-release-SW0058_ULTRA_ID0111.20260918-2737.app.tar.gz-prepared'})
        other=self.store.add_asset(self.other,'package','same-name',{'filename':'other-private-package'})
        self.create(own['id'])
        self.assertIn('SW0058 · 构建 2737',self.tasks.text(self.user))
        self.assertNotIn('other-private-package',self.tasks.text(self.user))
        self.create(other['id'])
        self.assertNotIn('other-private-package',self.tasks.text(self.user))

    def test_directory_query_groups_same_result_path_for_source_and_evaluation(self):
        task,job=self.create('source','SUCCEEDED');path=job['result']['task'] if job.get('result') else '/mnt/data/bench-eval/tasks/'+job['id']
        evaluation,_=self.store.submit(self.user,'chat','evaluation',{'action':'evaluate','source_job':job['id']})
        self.store.finish(evaluation,'PARTIAL_SUCCESS',{'task':path})
        value=directories(self.store,self.user)
        self.assertEqual(value.count(path),1)
        self.assertIn('独立评测',value);self.assertIn('回灌任务',value)
