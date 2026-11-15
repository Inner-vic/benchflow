"""Effective version inheritance and admission safety, without hardware."""
import copy
import json
import time
import unittest
from unittest.mock import Mock,patch

import test_bindings as bindings
import test_attachment_intake as files
from bench_gateway.autoflow import AutoFlow
from bench_gateway.core import GatewayError,Store
from bench_gateway.execution_identity import identity,cache_base,EquivalentExecution
from bench_gateway.incremental import InputBatch,intake_blocked
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks


class IncrementalInputsTests(unittest.TestCase):
    def setUp(self):
        self.c=bindings.BindingTests();self.c.setUp();self.addCleanup(self.c.tearDown)
        self.c.cfg.update(auto_execute_on_complete=True,intake_settle_seconds=0)
        self.tasks=Tasks(self.c.store);self.intake=Intake(self.c.store)

    def base(self,state='SUCCEEDED',yaml=True):
        c=self.c
        if not yaml:self.intake.update(c.user,{'yaml_request':'保留原配置'})
        c.preparation=c.make_preparation()
        c.receipt['path']=c.store.remote_root(c.user,c.cfg['remote_users_root'])+'/preparations/'+c.preparation['id']+'/binding.json'
        c.change_result(lambda r:r.update(yaml_preparation={'status':'PREPARED','outputs':[
            {'target':'config/a.yaml','path':c.store.remote_root(c.user,c.cfg['remote_users_root'])+'/preparations/old/a.yaml',
             'sha256':'c'*64,'before_sha256':'d'*64,'semantic_sha256':'e'*64,'before_semantic_sha256':'f'*64}]} if yaml else {}))
        baseline=c.bind();payload={'action':'replay','assets':baseline['data']['assets'],'baseline':baseline,'scenes':[1,2],'evaluate_after':False}
        old=self.tasks.current(c.user)
        job,_=c.store.submit(c.user,c.session,'base',payload);self.tasks.link(c.user,job)
        c.store.finish(job,state,{'task':'test-result'})
        return self.tasks.get(c.user,old['id']),c.store.job(c.user,job['id'])

    def ready(self):
        c=self.c;baseline=c.bind()
        c.change_result(lambda r:r.update(adapter_binding={'state':'READY_FOR_GATE','baseline_name':baseline['name']}))
        flow=AutoFlow(c.cfg,c.store);flow.arm(c.user,c.session);flow.tick()
        return flow

    def test_yaml_only_uses_exact_frozen_versions_and_preserves_existing_yaml(self):
        old,job=self.base();c=self.c
        c.store.add_asset(c.user,'package','A',{'sha256':'9'*64})
        self.intake.update(c.user,{'yaml_request':'config/a.yaml other.flag=false'})
        new=self.tasks.current(c.user)
        self.assertNotEqual(old['id'],new['id'])
        self.assertEqual(c.package['id'],new['data']['package'])
        self.assertEqual(c.sos['camera_so']['id'],new['data']['camera_so'])
        self.assertIn('/preparations/old/a.yaml',new['data']['yaml_request'])
        self.assertIn('other.flag=false',new['data']['yaml_request'])
        self.assertEqual('1,2',new['data']['scene_scope'])
        self.assertEqual(old,self.tasks.get(c.user,old['id']))
        self.assertEqual(job,c.store.job(c.user,job['id']))

    def test_new_app_reuses_sensors_as_candidates_and_drops_old_yaml_m100(self):
        self.base();c=self.c
        self.intake.update(c.user,{'package':'https://example.test/new.app.tar.gz'})
        draft=self.intake.get(c.user)
        self.assertEqual(c.sos['camera_so']['id'],draft['camera_so'])
        self.assertEqual(c.sos['lidar_so']['id'],draft['lidar_so'])
        for key in ('m100_so','yaml_request','assembly_ref'):self.assertNotIn(key,draft)
        self.assertEqual('build_if_missing',draft['m100_mode'])

    def test_successful_base_is_owner_scoped_when_no_material_draft(self):
        self.base();c=self.c
        with c.store.connect() as db:
            db.execute('DELETE FROM material_drafts WHERE owner=?',(c.user,))
            db.execute('DELETE FROM task_focus WHERE owner=?',(c.user,))
        self.intake.update(c.user,{'yaml_request':'config/b.yaml flag=true'})
        self.assertEqual(c.package['id'],self.intake.get(c.user)['package'])
        self.intake.update(c.other,{'yaml_request':'config/b.yaml flag=true'})
        self.assertNotIn('package',self.intake.get(c.other))
        self.assertEqual('共享18场景',self.intake.get(c.other)['dataset'])

    def test_sensor_change_invalidates_automatically_built_m100_but_yaml_does_not(self):
        c=self.c;self.intake.update(c.user,{'m100_mode':'build_if_missing'})
        self.base();self.intake.update(c.user,{'yaml_request':'config/b.yaml flag=true'})
        self.assertEqual(c.sos['m100_so']['id'],self.intake.get(c.user)['m100_so'])
        self.intake.update(c.user,{'camera_so':'new-camera'})
        self.assertNotIn('m100_so',self.intake.get(c.user))
        self.assertEqual('build_if_missing',self.intake.get(c.user)['m100_mode'])

    def test_unsubmitted_yaml_requirements_accumulate_and_explicit_cancel_clears(self):
        c=self.c;self.intake.update(c.user,{'yaml_request':'config/b.yaml flag=false'})
        self.assertIn('debug.enable=true',self.intake.get(c.user)['yaml_request'])
        self.assertIn('flag=false',self.intake.get(c.user)['yaml_request'])
        self.intake.update(c.user,{'yaml_request':'保留原配置'})
        self.assertNotIn('yaml_request',self.intake.get(c.user))

    def test_same_material_redeclaration_does_not_change_task_revision(self):
        c=self.c;old=self.tasks.current(c.user)
        self.intake.update(c.user,{'package':'A'})
        self.assertEqual(old,self.tasks.current(c.user))

    def test_repeated_whole_yaml_files_use_latest_source_without_model(self):
        from bench_gateway.yaml_plan import plan
        request='用工作站文件 /data/old.yaml 替换 APP 内 config/a.yaml；后续修改（同一字段以此为准）：用工作站文件 /data/new.yaml 替换 APP 内 config/a.yaml'
        with patch('bench_gateway.yaml_plan.subprocess.run') as model:result=plan({},request)
        self.assertEqual('READY',result['status']);self.assertEqual(1,len(result['operations']))
        self.assertEqual('/data/new.yaml',result['operations'][0]['source']);model.assert_not_called()

    def test_effective_identity_ignores_comment_changes_and_controller_metadata(self):
        _,job=self.base();c=self.c;first=identity(c.store,c.user,job['payload'])
        c.change_result(lambda r:r['yaml_preparation']['outputs'][0].update(sha256='a'*64))
        changed=copy.deepcopy(job['payload']);changed['baseline']['data']['binding']['implementation_hashes']={'changed':'9'*64}
        second=identity(c.store,c.user,changed)
        self.assertNotEqual(first['byte_digest'],second['byte_digest'])
        self.assertEqual(first['semantic_digest'],second['semantic_digest'])
        changed['scenes']=[1]
        self.assertNotEqual(first['semantic_digest'],identity(c.store,c.user,changed)['semantic_digest'])

    def test_identity_without_yaml_equals_a_semantically_unchanged_yaml(self):
        _,job=self.base(yaml=False);c=self.c;first=identity(c.store,c.user,job['payload'])
        c.change_result(lambda r:r.update(yaml_preparation={'status':'PREPARED','outputs':[{'target':'config/a.yaml','sha256':'c'*64,'before_sha256':'d'*64,'semantic_sha256':'e'*64,'before_semantic_sha256':'e'*64}]}))
        self.assertEqual(first['semantic_digest'],identity(c.store,c.user,job['payload'])['semantic_digest'])

    def test_input_batch_blocks_validation_and_commit_then_releases(self):
        c=self.c;flow=self.ready();original=Store.submit;batch=[]
        def arriving(store,*a,**kw):
            batch.append(InputBatch(store,c.user,'files:two.yaml'));return original(store,*a,**kw)
        with patch.object(Store,'submit',arriving):flow.tick()
        self.assertEqual([],c.store.jobs(c.user))
        batch[0].finish(True,True);flow.tick()
        self.assertEqual(1,len(c.store.jobs(c.user)))

    def test_failed_batch_is_not_cleared_by_queries_or_unrelated_files(self):
        c=self.c;first=InputBatch(c.store,c.user,'files:a.yaml');first.finish(False)
        InputBatch(c.store,c.user).finish(True)
        InputBatch(c.store,c.user,'files:b.yaml').finish(True,True)
        with c.store.connect() as db:self.assertTrue(intake_blocked(db,c.user))
        InputBatch(c.store,c.user,'files:a.yaml').finish(True,True)
        with c.store.connect() as db:self.assertFalse(intake_blocked(db,c.user))

    def test_abandoned_batch_remains_blocked_and_user_can_explicitly_resume(self):
        c=self.c;batch=InputBatch(c.store,c.user)
        with c.store.connect() as db:
            db.execute('UPDATE input_batches SET updated=0 WHERE token=?',(batch.token,))
            self.assertTrue(intake_blocked(db,c.user))
        flow=AutoFlow(c.cfg,c.store);self.assertIn('上次材料接收未完成',flow.receipt(c.user))
        flow.control(c.user,c.session,False)
        with c.store.connect() as db:self.assertFalse(intake_blocked(db,c.user))

    def test_pause_after_a_completed_job_pauses_next_materials_only(self):
        old,job=self.base();c=self.c;flow=AutoFlow(c.cfg,c.store)
        flow.control(c.user,c.session,True)
        self.intake.update(c.user,{'yaml_request':'config/b.yaml flag=false'});flow.arm(c.user,c.session);flow.tick()
        with c.store.connect() as db:self.assertEqual('PAUSED',db.execute('SELECT state FROM automatic_tasks').fetchone()[0])
        self.assertEqual(old,self.tasks.get(c.user,old['id']))
        self.assertEqual(job,c.store.job(c.user,job['id']))

    def test_replacing_app_while_paused_does_not_implicitly_resume(self):
        c=self.c;flow=AutoFlow(c.cfg,c.store);flow.control(c.user,c.session,True)
        self.intake.update(c.user,{'package':'https://example.test/new.app.tar.gz'})
        flow.arm(c.user,c.session);flow.tick();current=self.tasks.current(c.user)
        with c.store.connect() as db:self.assertEqual('PAUSED',db.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(current['id'],)).fetchone()[0])
        self.assertEqual([],c.store.jobs(c.user))

    def seed_equivalent(self,state='SUCCEEDED',force=False,immediate=True):
        c=self.c;flow=self.ready();baseline=c.bind()
        payload={'action':'replay','assets':baseline['data']['assets'],'baseline':baseline,'scenes':[1,2],'evaluate_after':False}
        job,_=c.store.submit(c.user,c.session,'equivalent',payload);c.store.finish(job,state,{'task':'retained'})
        cache_base(c.store,c.user,job['id']);task=self.tasks.current(c.user)
        with c.store.connect() as db:
            db.execute('UPDATE task_lineage SET base_job_id=?,force_run=? WHERE task_id=?',(job['id'] if immediate else 'different-current-version',int(force),task['id']))
        return flow,job

    def test_effectively_identical_base_is_reported_without_new_hardware_job(self):
        flow,old=self.seed_equivalent();c=self.c;flow.tick()
        self.assertEqual(1,len(c.store.jobs(c.user)))
        self.assertEqual('NO_CHANGE',self.tasks.current(c.user)['state'])
        self.assertIn('无有效变更',c.service.task_review(c.user))

    def test_explicit_repeat_can_rerun_same_inputs_but_unknown_outcome_stays_blocked(self):
        flow,old=self.seed_equivalent(force=True);c=self.c;flow.tick()
        self.assertEqual(2,len(c.store.jobs(c.user)))

    def test_unknown_outcome_is_not_bypassed_by_force(self):
        flow,old=self.seed_equivalent(state='NEEDS_REVIEW',force=True);c=self.c;flow.tick()
        self.assertEqual(1,len(c.store.jobs(c.user)))
        with c.store.connect() as db:self.assertEqual('BLOCKED',db.execute('SELECT state FROM automatic_tasks').fetchone()[0])

    def test_return_to_a_historical_configuration_after_another_version_can_execute(self):
        flow,old=self.seed_equivalent(immediate=False);c=self.c;flow.tick()
        self.assertEqual(2,len(c.store.jobs(c.user)))



    def test_task_direct_package_edit_clears_old_m100_keeps_sensor_candidates(self):
        c=self.c;old=self.tasks.current(c.user)
        self.tasks.update(c.user,old['id'],{'package':'https://example.test/new.app.tar.gz'})
        d=self.tasks.current(c.user)['data']
        self.assertNotIn('m100_so',d);self.assertEqual('build_if_missing',d['m100_mode'])
        self.assertEqual(old['data']['camera_so'],d['camera_so']);self.assertEqual(old['data']['lidar_so'],d['lidar_so'])

    def test_new_app_cannot_echo_old_m100_reference_from_previous_form(self):
        c=self.c;old=self.tasks.current(c.user)
        self.intake.update(c.user,{'package':'https://example.test/new.app.tar.gz','m100_so':old['data']['m100_so']})
        d=self.tasks.current(c.user)['data'];self.assertNotIn('m100_so',d)
        self.assertEqual('build_if_missing',d['m100_mode'])


class AttachmentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.c=files.AttachmentIntakeTests();self.c.setUp();self.addCleanup(self.c.tearDown)
        self.c.cfg.update(auto_execute_on_complete=True)

    def test_new_message_same_bytes_does_not_create_or_revise_task(self):
        c=self.c;c.inbox.receive(c.user,c.session,str(c.file()))
        old=Tasks(c.store).current(c.user)
        reply=c.inbox.receive(c.user,c.session,str(c.file(mid='om_duplicate_file_002')))
        self.assertIn('内容与当前材料一致',reply)
        self.assertEqual(old,Tasks(c.store).current(c.user));self.assertEqual(1,c.registry.ssh.upload.call_count)

    def test_caption_hold_is_honored_before_upload_and_preserved_afterwards(self):
        c=self.c;p=c.file('common_param.yaml',b'a: true\n')
        reply=c.inbox.prompt(c.user,c.session,'先保存，不要执行'+files.TRAILER+str(p)+')')
        self.assertIn('自动提交已暂停',reply)
        with c.store.connect() as db:self.assertEqual('PAUSED',db.execute('SELECT state FROM automatic_tasks').fetchone()[0])

    def test_two_yaml_files_with_one_target_are_rejected_before_transfer(self):
        c=self.c;a=c.file('first.yaml',b'a: true\n');b=c.file('second.yaml',b'b: true\n',mid='om_other_file_002')
        with self.assertRaises(GatewayError):c.inbox.prompt(c.user,c.session,'替换 etc/uroad/config/test.yaml'+files.TRAILER+str(a)+', '+str(b)+')')
        c.registry.ssh.upload.assert_not_called()

    def test_unknown_caption_is_held_and_not_silently_ignored(self):
        c=self.c;p=c.file('common_param.yaml',b'a: true\n')
        reply=c.inbox.prompt(c.user,c.session,'另一个要求暂时不能确定'+files.TRAILER+str(p)+')')
        self.assertIn('尚未完整解析',reply)
        with c.store.connect() as db:self.assertEqual('PAUSED',db.execute('SELECT state FROM automatic_tasks').fetchone()[0])


if __name__=='__main__':unittest.main()
