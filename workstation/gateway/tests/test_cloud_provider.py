import copy
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from bench_gateway import cloud_provider as cloud
from bench_gateway.core import GatewayError, owner_key
from bench_gateway.executor import Executor
import test_workstation_evaluation as workstation_fixtures


class CloudProviderTests(unittest.TestCase):
    setUp=workstation_fixtures.WorkstationEvaluationTests.setUp

    def policy(self):
        profile=json.loads((Path(__file__).resolve().parents[1]/'config/cloud-evaluation-profile.json').read_text())
        self.cfg['cloud_evaluation_profiles']=[{'effective_after':self.job['created']-1,'profile':profile}]
        return profile

    def receipt(self):
        profile=self.policy()
        cid,root,prefix,remote=cloud.paths(self.cfg,self.store,self.job,profile)
        root.mkdir(parents=True)
        path=root/'evaluation-report.txt'
        path.write_bytes(self.raw)
        parser=cloud.workstation._module(self.cfg,'scripts/runtime/parse_eval_report.py')
        parsed=parser.parse_report(path)
        coverage=[{'index':1,'scene':'scene-a','completed_frames':2,'truth_frames':2}]
        return {'kind':'lpai-mounted-v1','job_id':self.job['id'],'source_job':self.run,
                'source_task':self.task,'owner_key':owner_key(self.user),'profile_sha256':profile['profile_sha256'],
                'remote_attempt':remote,'local_report':str(path),'report_sha256':hashlib.sha256(self.raw).hexdigest(),
                'native_receipt':{'run_id':cid,'owner':self.user,'source_run':self.run,
                    'report_sha256':hashlib.sha256(self.raw).hexdigest(),'metrics':parsed['metrics'],
                    'scene_coverage':coverage,'source_data_hash_passes':0,'input_content_verified':False,'native_seconds':1.0}}

    def test_preexisting_jobs_keep_the_workstation_backend(self):
        profile=self.policy()
        self.cfg['cloud_evaluation_profiles'][0]['effective_after']=self.job['created']+1
        with patch('bench_gateway.workstation_evaluation.evaluate',return_value={'legacy':True}) as existing:
            self.assertEqual(Executor(self.cfg,self.store).evaluate(self.job),{'legacy':True})
            existing.assert_called_once()

    def test_backend_policy_epochs_are_stable_and_hash_checked(self):
        first=self.policy()
        second=copy.deepcopy(first)
        second['gt_root']+='/new-version'
        second['profile_sha256']=hashlib.sha256(cloud.canonical({k:v for k,v in second.items() if k!='profile_sha256'})).hexdigest()
        self.cfg['cloud_evaluation_profiles'].append({'effective_after':self.job['created']+100,'profile':second})
        self.assertEqual(cloud.profile_for_job(self.cfg,self.job),first)
        self.assertEqual(cloud.profile_for_job(self.cfg,{'created':self.job['created']+101}),second)
        self.cfg['cloud_evaluation_profiles'][0]['profile']['gt_root']+='tampered'
        with self.assertRaises(GatewayError):
            cloud.profile_for_job(self.cfg,self.job)

    def test_original_txt_and_exact_source_coverage_are_required(self):
        receipt=self.receipt()
        result=cloud.validate_receipt(self.cfg,self.store,self.job,receipt,source=self.source)
        self.assertEqual(result['evaluation_state'],'COMPLETE')
        self.assertEqual(result['execution_backend'],'lpai-mounted-v1')
        self.assertEqual(result['scene_coverage'],receipt['native_receipt']['scene_coverage'])

    def test_other_owner_and_modified_metrics_cannot_be_delivered(self):
        receipt=self.receipt()
        for field,value in (('owner',self.other),('metrics',{'candidate_frames':999})):
            altered=copy.deepcopy(receipt)
            altered['native_receipt'][field]=value
            with self.subTest(field=field),self.assertRaises(GatewayError):
                cloud.validate_receipt(self.cfg,self.store,self.job,altered,source=self.source)

    def test_collection_never_exports_or_submits_a_missing_attempt(self):
        self.policy()
        with patch.object(cloud.workstation,'resolve_task',return_value=self.source),\
             patch.object(cloud,'export_predictions') as export,patch.object(cloud,'transport') as transport:
            with self.assertRaisesRegex(GatewayError,'续收不会创建'):
                cloud.evaluate(self.cfg,self.store,self.job,collect_only=True)
            export.assert_not_called()
            transport.assert_not_called()

    def test_stopped_or_unrelated_receiver_is_not_treated_as_ready(self):
        profile=self.policy()
        good={'profile_sha256':profile['profile_sha256'],'instance':profile['instance'],'state':'READY','checked_at':time.time()}
        for updates in ({'checked_at':time.time()-120},{'profile_sha256':'0'*64},{'state':'STOPPED'}):
            with self.subTest(updates=updates),patch.object(cloud,'transport',return_value={**good,**updates}),self.assertRaises(GatewayError):
                cloud.ready(self.cfg,profile)

    def test_recovery_requires_original_cloud_result_and_receipt(self):
        receipt=self.receipt()
        _,local,_,_=cloud.paths(self.cfg,self.store,self.job,self.policy())
        (local/'gateway-evaluation-receipt.json').write_text(json.dumps(receipt))
        with patch.object(cloud,'evaluate',return_value={'original':True}) as collect:
            self.assertEqual(Executor(self.cfg,self.store).evaluate_receipt(self.job,receipt),{'original':True})
            collect.assert_called_once_with(self.cfg,self.store,self.job,collect_only=True)
            with self.assertRaises(GatewayError):
                Executor(self.cfg,self.store).evaluate_receipt(self.job,{**receipt,'report_sha256':'0'*64})

    def test_cloud_status_query_never_creates_a_task_or_probes_the_bench(self):
        from bench_gateway.service import Service
        self.policy()
        before=self.store.jobs(self.user)
        with patch.object(cloud,'ready',return_value={'state':'READY'}) as health,\
             patch.object(cloud.workstation,'probe_runtime') as old_probe:
            reply=Service(self.cfg).handle(self.session,'评测环境',{'action':'evaluation_status'})
        self.assertIn('云端评测',reply)
        self.assertIn('自动匹配场景',reply)
        health.assert_called_once()
        old_probe.assert_not_called()
        self.assertEqual(before,self.store.jobs(self.user))

    def test_collect_cli_selects_saved_backend_and_never_starts_an_evaluation(self):
        from bench_gateway.cli import main
        self.policy()
        self.store.finish(self.job,'NEEDS_REVIEW',{'error':'connection interrupted'})
        argv=['gateway','collect-evaluation','--user',self.user,'--job',self.job['id']]
        result={'task':self.task,'evaluation_state':'PARTIAL','warnings':['VISION_GT_MISSING']}
        with patch('sys.argv',argv),patch('bench_gateway.cli.load',return_value=self.cfg),\
             patch.object(cloud,'evaluate',return_value=result) as collect,\
             patch.object(cloud.workstation,'evaluate') as other,patch('builtins.print'):
            main()
        collect.assert_called_once()
        self.assertTrue(collect.call_args.kwargs['collect_only'])
        other.assert_not_called()
        self.assertEqual(self.store.job(self.user,self.job['id'])['state'],'PARTIAL_SUCCESS')


if __name__=='__main__':
    unittest.main()
