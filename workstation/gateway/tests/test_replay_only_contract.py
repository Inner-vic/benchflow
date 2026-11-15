import copy,json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from bench_gateway import candidate_adapter as adapter
from test_candidate_adapter import fixture
import workflow_planner as planner


class ReplayOnlyTests(unittest.TestCase):
    def test_system_compiled_m100_is_rebuilt_when_mode_or_inputs_change(self):
        from test_preparation_progress import PreparationProgressTests
        from bench_gateway.preparation import MaterialCheck
        for goal,compiled_flag,changed_sensor,expected_calls in [('replay_and_evaluate',False,False,1),('replay',False,False,0),('replay',False,True,1)]:
            h=PreparationProgressTests();h.setUp();self.addCleanup(h.doCleanups)
            h.cfg['replay_only_no_dump_enabled']=True
            h.running(goal=goal,m100_mode='provided',m100_so='https://example.test/liburoad_m100.so')
            service,fetch=h.material_service(verify_phases=False)
            def supplied(*args,**kwargs):
                asset=fetch(*args,**kwargs)
                if asset['id']=='asset-m100_so':
                    asset['data'].update(m100_automatic_build=True,eval_dump=compiled_flag,compiled_for_package_sha256='a'*64,
                        compiled_sensor_sha256={'camera_so':'a'*64,'lidar_so':('b' if changed_sensor else 'a')*64})
                return asset
            with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.asset_fetch.fetch',side_effect=supplied),patch('bench_gateway.m100_build.build',return_value=(h.asset('m100_so'),{'state':'BUILT'})) as build:
                result=MaterialCheck(h.cfg,h.store).run(h.job)
            self.assertEqual('STATIC_PASSED',result['status']);self.assertEqual(expected_calls,build.call_count)
            if expected_calls:self.assertEqual(goal,build.call_args.kwargs['goal'])

    def test_render_keeps_legacy_outputs_and_adds_separate_replay_target(self):
        template,candidate=fixture();legacy=adapter.render(template,candidate,'old','actor',[1])
        self.assertEqual(['workstation_predictions'],legacy['job']['workflow']['targets'])
        candidate['replay_only']=True;before=copy.deepcopy((template,candidate))
        value=adapter.render(template,candidate,'only','actor',[1])
        self.assertEqual(before,(template,candidate))
        self.assertEqual(['replay_complete'],value['job']['workflow']['targets'])
        self.assertEqual([],value['identity']['required_output_types'])
        self.assertIn('("eval_dump", "enable"): "OFF"',value['driver'])
        self.assertNotIn('("eval_dump.gt_trajectory", "enable"):',value['driver'])
        self.assertIn('replay_only_sweep',value['adapter'])
        self.assertIn('return ()',value['adapter'])

    def test_frame_and_cloud_client_derivations_compose(self):
        for mode,raw in [('frame_by_frame',False),('stream',True),('frame_by_frame',True)]:
            template,candidate=fixture();candidate.update(replay_only=True,replay_mode=mode)
            if raw:candidate['dataset']['prepared_scenes'][0]['replay_input_format']='cloud-original-v1'
            value=adapter.render(template,candidate,'only-'+mode+'-'+str(raw),'actor',[1])
            compile(value['adapter'],'derived','exec');compile(value['driver'],'driver','exec')
            self.assertIn('gateway_replay_tools',value['adapter'])
            self.assertIn('str(replay_only_sweep(run_dir)), "--from-job"',value['adapter'])

    def test_new_target_requires_bench_but_not_prediction_collection(self):
        template,candidate=fixture();candidate['replay_only']=True
        job=adapter.render(template,candidate,'only','actor',[1])['job']
        self.assertEqual([],planner.validate_workflow(job))
        self.assertTrue(planner.requires_bench(job));self.assertFalse(planner.uses_personal_lpai(job))
        steps=planner.build_target_steps(job,stage_ready=True,replay_ready=True)
        states={s['state'] for s in steps if s['requested']}
        self.assertTrue({'REPLAY','RESTORE','ACTIVATE'} <= states)
        self.assertFalse({'COLLECT_WORKSTATION','EVALUATE','PUBLISH_LPAI'} & states)
        for targets,sources in [(['replay_complete','evaluation_report'],{}),(['replay_complete'],{'workstation_predictions':'/tmp/x'})]:
            changed=copy.deepcopy(job);changed['workflow'].update(targets=targets,sources=sources)
            self.assertTrue(planner.validate_workflow(changed))
        legacy={'runner_protocol':'1.0'}
        self.assertEqual(planner.TARGETS,planner.requested_targets(legacy))

    def make_delivery(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);base=Path(t.name)
        native=base/'native';bench=base/'bench';owner=base/'users/u_test';owner.mkdir(parents=True)
        root=native/'runs/r1';root.mkdir(parents=True)
        evidence=bench/'adapters/r1/evidence/r1';evidence.mkdir(parents=True)
        read=lambda path:json.loads(Path(path).read_text())
        def write(path,value):Path(path).write_text(json.dumps(value))
        write(root/'state.json',{'state':'COMPLETED_TO_REQUESTED_GOAL'})
        write(root/'restore-verification.json',{'restore_ok':True})
        row={'scene':'one','state':'REPLAY_COMPLETE','transport':'Recorder-native-camera-lidar-stream','recorded_lidar_frames':10,'attempt_index':1}
        write(evidence/'batch-result.json',{'expected':1,'completed':[row]})
        attempt=evidence/'one/attempt-1';attempt.mkdir(parents=True)
        write(attempt/'transport-result.json',row);write(attempt/'recorder-final-status.json',{'state':'completed'})
        (attempt/'native-stream-replay.txt').write_text('synthetic unit fixture; no hardware executed')
        (root/'log.txt').write_text('actual replay log')
        p={'run_id':'r1','task_id':'task1','scenes':[{'scene':'one'}],'replay_owner_root':str(owner)}
        return SimpleNamespace(ROOT=native,BASE=bench,read=read,write=write),p,root,evidence

    def test_delivery_is_logs_not_a_prediction_product_and_recovery_is_readonly(self):
        m,p,root,evidence=self.make_delivery();result=adapter.replay_only_delivery(m,p,0)
        self.assertEqual('replay_evidence',result['product_kind']);self.assertNotIn('product',result)
        record=m.read(result['receipt']);self.assertFalse(record['predictions_generated']);self.assertFalse(record['evaluation_executed'])
        self.assertTrue((Path(result['task'])/'runner/log.txt').is_file())
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)
        p['resume_delivery']=True;self.assertEqual(result,adapter.replay_only_delivery(m,p,0))
        (Path(result['task'])/'runner/log.txt').write_text('changed')
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)

    def test_unknown_incomplete_or_unrestored_runs_cannot_be_delivered(self):
        for name,value in [('state.json',{'state':'RUNNING'}),('restore-verification.json',{'restore_ok':False})]:
            m,p,root,evidence=self.make_delivery();m.write(root/name,value)
            with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)
            self.assertFalse((Path(p['replay_owner_root'])/'tasks/task1').exists())
        m,p,root,evidence=self.make_delivery();m.write(evidence/'batch-result.json',{'expected':1,'completed':[]})
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)
        m,p,root,evidence=self.make_delivery()
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,1)

    def test_delivery_rejects_links_and_unsafe_task_names(self):
        m,p,root,evidence=self.make_delivery();(root/'link').symlink_to(root/'log.txt')
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)

    def test_missing_native_or_changed_scene_receipt_cannot_be_delivered(self):
        m,p,root,evidence=self.make_delivery();(evidence/'one/attempt-1/native-stream-replay.txt').unlink()
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)

    def test_dump_disabled_job_cannot_be_upgraded_to_evaluation(self):
        from test_execution_controls import ExecutionControlTests
        h=ExecutionControlTests();h.setUp();self.addCleanup(h.doCleanups);job=h.job(False)
        payload=copy.deepcopy(job['payload']);payload['baseline']['data']['execution_policy']={'replay_only':True}
        with h.store.connect() as c:c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),job['id']))
        before=h.store.jobs(h.user);result=h.control(True)
        self.assertEqual('UNSUPPORTED',result['state']);self.assertEqual(before,h.store.jobs(h.user))

    def test_completed_card_and_service_do_not_offer_posthoc_evaluation(self):
        from test_card_ui import CardUITests
        from bench_gateway import card_ui,task_cards
        from bench_gateway.core import GatewayError
        h=CardUITests();h.setUp();self.addCleanup(h.doCleanups);h.cfg['replay_only_no_dump_enabled']=True
        job=h.freeze();payload=copy.deepcopy(job['payload']);payload['evaluate_after']=False
        with h.store.connect() as c:c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),job['id']))
        job=h.store.job(h.user,job['id']);h.store.complete_replay(job,{'state':'PASS','task':'/fixture/logs','run_id':job['id']})
        done=h.store.job(h.user,job['id']);value=task_cards.snapshot(h.store,done)
        value['card_ui']=card_ui.describe(h.cfg,h.store,h.tasks.current(h.user));card=task_cards.render(value)
        def actions(node):
            if isinstance(node,dict):
                if 'value' in node and isinstance(node['value'],dict) and 'action' in node['value']:yield node['value']['action']
                for v in node.values():yield from actions(v)
            elif isinstance(node,list):
                for v in node:yield from actions(v)
        self.assertNotIn('evaluate',list(actions(card)))
        with self.assertRaises(GatewayError):h.service.handle(h.session,'评测任务',{'action':'evaluate','task_id':job['id']})
        self.assertEqual(1,len(h.store.jobs(h.user)))
        m,p,root,evidence=self.make_delivery();m.write(evidence/'one/attempt-1/transport-result.json',{'scene':'wrong'})
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)
        m,p,root,evidence=self.make_delivery();p['task_id']='../escape'
        with self.assertRaises(ValueError):adapter.replay_only_delivery(m,p,0)


if __name__=='__main__':unittest.main()
