import json
import unittest

import test_task_cards as fixtures
from bench_gateway.task_cards import snapshot, render, workflow, scene_progress


class TaskProgressTests(unittest.TestCase):
    setUp=fixtures.TaskCardTests.setUp
    emit=fixtures.TaskCardTests.emit
    def value(self):
        return snapshot(self.store, self.store.job(self.owner, self.job['id']))

    def statuses(self):
        return [s['status'] for s in workflow(self.value())]

    def test_steps_follow_observed_business_nodes(self):
        self.assertEqual(['pending']*3+['skipped'], self.statuses())
        self.store.claim(); self.store.stage(self.job, '小场景预检中')
        self.assertEqual(['active', 'pending', 'pending', 'skipped'], self.statuses())
        self.emit('SOFTWARE_READY')  # A formal-run event cannot complete software preparation.
        self.assertEqual('active', self.statuses()[0])
        from bench_gateway.progress import event
        event(self.store, self.owner, self.job['id'], self.job['id']+'-smoke',
              {'schema_version':1, 'run_id':self.job['id']+'-smoke', 'kind':'SOFTWARE_READY'})
        self.assertEqual(['done', 'active', 'pending', 'skipped'], self.statuses())
        self.store.stage(self.job, '小场景预检通过')
        self.assertEqual(['done', 'done', 'active', 'skipped'], self.statuses())

    def test_all_scenes_are_not_a_terminal_receipt(self):
        self.store.claim(); self.store.stage(self.job, '正式回灌中')
        self.emit('BATCH', completed=18, total=18)
        value=self.value(); content=json.dumps(render(value), ensure_ascii=False)
        self.assertIn('18/18', content); self.assertIn('100%', content)
        self.assertIn('正在收尾，尚未完成', content)
        self.assertEqual('active', workflow(value)[2]['status'])
        self.emit('PHASE', phase='核验恢复')
        self.assertEqual(['done', 'done', 'active', 'skipped'], self.statuses())
        self.store.finish(self.job, 'SUCCEEDED', {'task':'/data/tasks/'+self.job['id']})
        self.assertEqual(['done']*3+['skipped'], self.statuses())

    def test_bar_uses_scene_count_and_late_events_do_not_regress(self):
        self.store.claim(); self.store.stage(self.job, '正式回灌中')
        self.emit('BATCH', completed=6, total=18)
        bar=scene_progress(self.value())
        self.assertIn('6/18', bar); self.assertIn('33%', bar)
        self.assertEqual(4, bar.count('▰')); self.assertEqual(8, bar.count('▱'))
        self.emit('PHASE', phase='收集工作站预测')
        self.emit('BATCH', completed=2, total=18)
        self.assertEqual(6, self.value()['completed']); self.assertEqual('active', self.statuses()[2])

    def test_invalid_foreign_and_smoke_counts_do_not_fill_formal_bar(self):
        self.store.claim(); self.store.stage(self.job, '小场景预检中')
        self.emit('BATCH', completed=18, total=17)
        from bench_gateway.progress import event
        for run in (self.job['id']+'-smoke', 'other-run'):
            event(self.store, self.owner, self.job['id'], run,
                  {'schema_version':1, 'run_id':run, 'kind':'BATCH', 'completed':18, 'total':18})
        self.assertEqual(0, self.value()['completed'])

    def test_failure_freezes_actual_progress_without_completing_future_steps(self):
        self.store.claim(); self.store.stage(self.job, '正式回灌中')
        self.emit('BATCH', completed=6, total=18)
        result=self.store.job(self.owner, self.job['id'])['result']
        self.store.finish(self.job, 'FAILED', result)
        self.assertEqual(['done', 'done', 'blocked', 'skipped'], self.statuses())
        self.assertIn('33%', scene_progress(self.value()))

    def test_optional_evaluation_is_a_separate_observed_node(self):
        self.store.claim()
        job=self.store.job(self.owner, self.job['id']);job['payload']['evaluate_after']=True
        job['state']='POSTPROCESSING';job['result']={'task':'/data/retained'}
        value=snapshot(self.store, job)
        self.assertEqual(['done']*3+['active'], [s['status'] for s in workflow(value)])
        self.assertEqual('结果评测', workflow(value)[3]['label'])
        job['state']='PARTIAL_SUCCESS'
        value=snapshot(self.store, job)
        self.assertEqual(['done']*3+['blocked'], [s['status'] for s in workflow(value)])

    def test_simulated_and_cancelled_tasks_never_show_completed_nodes(self):
        for state in ('SIMULATED', 'CANCELLED'):
            job=self.store.job(self.owner, self.job['id']);job.update(state=state,result={'execution_progress':{'completed':18}})
            value=snapshot(self.store, job)
            self.assertEqual(['pending']*3+['skipped'],[s['status'] for s in workflow(value)])
            self.assertNotIn('100%', scene_progress(value))

    def test_large_ranges_keep_bar_bounded_and_do_not_round_up_to_complete(self):
        self.store.claim();self.store.stage(self.job,'正式回灌中')
        job=self.store.job(self.owner, self.job['id']);job['payload']['scenes']=list(range(1,1001))
        job.update(state='RUNNING',result={'execution_progress':{'completed':999}})
        bar=scene_progress(snapshot(self.store, job))
        self.assertIn('99%', bar);self.assertNotIn('100%', bar)
        self.assertEqual(12, bar.count('▰')+bar.count('▱'));self.assertEqual(1,bar.count('▱'))


    def test_nodes_are_business_stages_with_optional_evaluation_visible(self):
        steps=workflow(self.value())
        self.assertEqual(['软件部署','启动预检','数据回灌','结果评测'],[x['label'] for x in steps])
        self.assertEqual('已跳过',steps[3]['hint'])
        self.assertFalse(any(x['label'] in ('开始','完成','核验') for x in steps))

    def test_single_scene_truthfully_shows_current_precheck_policy(self):
        self.store.claim();self.store.stage(self.job,'小场景预检中')
        from bench_gateway.progress import event
        event(self.store,self.owner,self.job['id'],self.job['id']+'-smoke',{'schema_version':1,'run_id':self.job['id']+'-smoke','kind':'SOFTWARE_READY'})
        job=self.store.job(self.owner,self.job['id']);job['payload']['scenes']=[1]
        value=snapshot(self.store,job)
        self.assertEqual('启动预检中',value['label']);self.assertIn('正式回灌 1 个场景',scene_progress(value))
        self.assertNotIn('0/1',scene_progress(value));self.assertEqual('active',workflow(value)[1]['status'])

    def test_material_check_does_not_claim_bench_deployment_has_started(self):
        value=self.value();value.update(input_phase=True,state='CHECKING')
        self.assertEqual(['pending']*3+['skipped'],[x['status'] for x in workflow(value)])

    def test_restoration_and_collection_are_replay_substates(self):
        self.store.claim();self.store.stage(self.job,'正式回灌中');self.emit('BATCH',completed=18,total=18)
        for phase,excerpt in [('恢复正式服务','恢复台架'),('核验恢复','台架状态'),('收集工作站预测','收集结果')]:
            self.emit('PHASE',phase=phase);value=self.value()
            self.assertEqual('回灌收尾',value['label']);self.assertIn(excerpt,value['detail'])
            self.assertEqual(['done','done','active','skipped'],[x['status'] for x in workflow(value)])

    def test_elapsed_is_retained_in_evidence_but_not_shown_on_the_card(self):
        self.store.claim();self.store.stage(self.job,'小场景预检中');value=self.value()
        self.assertTrue(value['started']);raw=json.dumps(render(value),ensure_ascii=False)
        self.assertNotIn('用时',raw);self.assertEqual(4,len(workflow(value)))

    def test_evaluation_completion_only_changes_the_evaluation_node(self):
        job=self.store.job(self.owner,self.job['id']);job['payload']['evaluate_after']=True
        job.update(state='SUCCEEDED',result={'report':'/report.txt','task':'/data/results'})
        value=snapshot(self.store,job)
        self.assertEqual(['done']*4,[x['status'] for x in workflow(value)])
        self.assertEqual('回灌与评测完成',value['label'])

    def test_elapsed_keeps_updating_during_optional_evaluation(self):
        job=self.store.job(self.owner,self.job['id']);job['payload']['evaluate_after']=True
        job.update(state='POSTPROCESSING',created=100,updated=150,result={'replay_completed_at':120,'task':'/data/results'})
        value=snapshot(self.store,job,now=300)
        self.assertEqual('3分0秒',value['elapsed']);self.assertEqual('active',workflow(value)[3]['status'])

    def test_existing_result_evaluation_has_no_fictitious_replay_nodes(self):
        job=self.store.job(self.owner,self.job['id']);job['payload']['action']='evaluate'
        job.update(state='RUNNING',result={});value=snapshot(self.store,job)
        self.assertEqual('结果评测中',value['label'])
        self.assertEqual([{'label':'结果评测','status':'active'}],workflow(value))
        self.assertIn('已有回灌结果',scene_progress(value))
        self.assertIn('仅评测',json.dumps(render(value)['body'],ensure_ascii=False))


if __name__=='__main__': unittest.main()
