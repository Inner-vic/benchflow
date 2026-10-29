import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_arrival_accuracy as runner
from arrival_report_payload import prepare


class ArrivalEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root/'config.json'

    def save(self, config):
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        return self.config_path

    def fixture(self, omit_column=None, jerk_frames=None, no_control_rows=False):
        inputs = self.root/'input'/'scene'
        inputs.mkdir(parents=True)
        fields=['odom_ts_ms','traj_ts_ms','payload_ctrl_ts_ms','publish_seq','dds_write_ok','speed_kph']
        fields += [f'{w}_{f}_0' for w in ('fl','fr') for f in ('dist_pre_mm','dist_out_mm','feat','start_dist')]
        fields=[k for k in fields if k!=omit_column]
        with (inputs/'sus_preview_timeline.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields)
            writer.writeheader()
            if not no_control_rows:
                for i in range(2):
                    values={k:1 for k in fields}
                    values.update({k:1000+10*i for k in ('odom_ts_ms','traj_ts_ms','payload_ctrl_ts_ms') if k in fields})
                    values['publish_seq']=i+1
                    writer.writerow(values)
        for w in ('FL','FR'):
            name = f's_InDatCalcFAS_WhlJerk{w}_mps3'
            (inputs/(name+'.json')).write_text(json.dumps({'signal_name': name, 'frames': jerk_frames if jerk_frames is not None else [{'ts_ns':1000000000,'physical':1},{'ts_ns':1010000000,'physical':1}]}), encoding='utf-8')
        product='/cache/run1/predictions'
        inventory=[{'path':'scene/sus_preview_timeline.csv','size':(inputs/'sus_preview_timeline.csv').stat().st_size,'sha256':runner.sha(inputs/'sus_preview_timeline.csv')}]
        from workflow_receipts import content_identity
        from prediction_transfer import inventory_digest
        product_id=content_identity({'product_type':'workstation_predictions','files':inventory})
        common={'product_identity':product_id,'inventory_sha256':inventory_digest(inventory)}
        docs={
            'task_index':{'state':'PASS','run_id':'run1','scene_count':1,'file_count':1,'canonical_product':product,'producer_receipt':product+'.bench-eval-product.json',
                          'scenes':[{'scene_id':'s001','scene':'scene','run_id':'run1','task_copy':'/tasks/run1/scenes/s001/predictions/scene'}],**common},
            'producer_receipt':{'state':'SUCCEEDED','product_type':'workstation_predictions','producer_run_id':'run1','workstation_product':product,'files':inventory,**common},
            'transfer_receipt':{'state':'WORKSTATION_PREDICTIONS_COLLECTED','run_id':'run1','producer_run_id':'run1','workstation_product':product,'product_receipt':product+'.bench-eval-product.json','file_count':1,**common},
            'run_state':{'state':'COMPLETED_TO_REQUESTED_GOAL','run_id':'run1'},
            'restore_receipt':{'restore_ok':True,'run_id':'run1'},
        }
        upstream={}
        for key,document in docs.items():
            p=self.root/(key+'.json')
            p.write_text(json.dumps(document),encoding='utf-8')
            upstream[key]={'path':str(p),'sha256':runner.sha(p)}
        receipt = self.root/'receipt.json'
        receipt.write_text(json.dumps({'run_id':'run1','product_identity':product_id,'files':[
            {'local':str(p),'sha256':runner.sha(p),'source':('/tasks/run1/scenes/s001/predictions/scene/'+p.name if p.suffix=='.csv' else '/truth/scene/original/xcp/BZCU/'+p.name)} for p in inputs.iterdir()]}), encoding='utf-8')
        core = self.root/'core.py'
        core.write_text('# audited fixture', encoding='utf-8')
        node = self.root/'node.exe'
        node.write_text('fixture', encoding='utf-8')
        runtime = self.root/'runtime'
        (runtime/'node_modules/@oai/artifact-tool').mkdir(parents=True)
        c={'enabled':True,'engine':runner.ENGINE,'source':{'input_root':str(inputs.parent),'receipt':str(receipt),
            'receipt_base':str(self.root),'run_id':'run1','product_identity':product_id,'truth_root':'/truth','upstream':upstream},'core':str(core),
            'node':str(node),'artifact_runtime_root':str(runtime),'output':str(self.root/'attempt')}
        return c, core, inputs

    def test_default_disabled_does_not_need_inputs_or_mutate(self):
        before=set(self.root.iterdir())
        p=runner.plan(self.save({}))
        self.assertEqual(p['state'],'NOT_REQUESTED')
        self.assertFalse(p['mutations_performed'])
        self.assertEqual(set(self.root.iterdir()),before|{self.config_path})

    def test_plan_validates_sources_without_creating_output(self):
        c,core,_=self.fixture()
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            p=runner.plan(self.save(c))
        self.assertEqual(p['state'],'PLAN_READY')
        self.assertEqual(p['scene_count'],1)
        self.assertEqual(p['required_formats'],['analysis_plan.json','arrival-accuracy.xlsx'])
        self.assertFalse(Path(c['output']).exists())

    def test_source_change_and_wrong_product_are_rejected(self):
        c,core,inputs=self.fixture()
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            original=c['source']['product_identity']
            c['source']['product_identity']='other'
            with self.assertRaisesRegex(ValueError,'product_identity mismatch'):runner.plan(self.save(c))
            c['source']['product_identity']=original
            (inputs/'sus_preview_timeline.csv').write_text('changed',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):runner.plan(self.save(c))

    def test_no_native_fallback_and_no_rule_overrides(self):
        c,core,_=self.fixture()
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            c['engine']='native_plugin'
            self.assertEqual(runner.plan(self.save(c))['state'],'BLOCKED')
            c['threshold']=700
            with self.assertRaisesRegex(ValueError,'rule overrides'):runner.plan(self.save(c))

    def test_output_overlap_and_overwrite_are_rejected(self):
        c,core,_=self.fixture()
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            Path(c['output']).mkdir()
            with self.assertRaisesRegex(ValueError,'already exists'):runner.plan(self.save(c))
            c['output']=str(self.root/'input/new-report')
            with self.assertRaisesRegex(ValueError,'separate'):runner.plan(self.save(c))

    def test_missing_xlsx_dependency_does_not_fall_back_to_html(self):
        c,core,_=self.fixture()
        c['node']=str(self.root/'missing-node')
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'HTML/CSV will not replace'):runner.plan(self.save(c))

    def test_nanosecond_epochs_are_text_before_javascript(self):
        epoch=1787194179922299314
        p=prepare({'state':'CALCULATED','contact_timestamp_ns':epoch,'samples':19,'error_mm':31.59})
        self.assertEqual(p['contact_timestamp_ns'],str(epoch))
        self.assertEqual(p['samples'],19)
        self.assertEqual(p['error_mm'],31.59)

    def test_reuse_result_requires_pinned_file_hash(self):
        c,core,_=self.fixture()
        result=self.root/'result.json'
        result.write_text('{}',encoding='utf-8')
        c['reuse_result']={'path':str(result),'sha256':'0'*64}
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'cached result file hash'):runner.plan(self.save(c))

    def test_header_only_and_missing_calculation_field_do_not_pass_preflight(self):
        c,core,_=self.fixture(no_control_rows=True)
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'no valid samples'):runner.plan(self.save(c))

    def test_field_used_by_real_parser_is_required_in_plan(self):
        c,core,_=self.fixture(omit_column='fl_feat_0')
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'missing timeline columns'):runner.plan(self.save(c))

    def test_jerk_duplicate_times_do_not_pass_preflight(self):
        c,core,_=self.fixture(jerk_frames=[{'ts_ns':1000000000,'physical':1},{'ts_ns':1000000000,'physical':2}])
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'not strictly increasing'):runner.plan(self.save(c))

    def test_incomplete_upstream_replay_is_rejected(self):
        c,core,_=self.fixture()
        spec=c['source']['upstream']['restore_receipt']
        Path(spec['path']).write_text(json.dumps({'run_id':'run1','restore_ok':False}),encoding='utf-8')
        spec['sha256']=runner.sha(spec['path'])
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'not complete'):runner.plan(self.save(c))

    def test_self_declared_download_identity_is_not_sufficient(self):
        c,core,_=self.fixture()
        c['source']['upstream']={}
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'existing task'):runner.plan(self.save(c))

    def test_wrong_jerk_scene_source_is_rejected(self):
        c,core,_=self.fixture()
        rp=Path(c['source']['receipt'])
        d=json.loads(rp.read_text(encoding='utf-8'))
        for item in d['files']:
            if item['local'].endswith('.json'):item['source']=item['source'].replace('/scene/','/other_scene/')
        rp.write_text(json.dumps(d),encoding='utf-8')
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            with self.assertRaisesRegex(ValueError,'jerk source'):runner.plan(self.save(c))

    def test_source_modified_after_plan_is_rejected_before_output_creation(self):
        c,core,inputs=self.fixture()
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            p=runner.plan(self.save(c))
        (inputs/'sus_preview_timeline.csv').write_text('changed after plan',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'changed after planning'):runner.execute(p)
        self.assertFalse(Path(c['output']).exists())

    def test_calculation_failure_is_retained_as_failed_attempt(self):
        c,core,_=self.fixture()
        with patch.object(runner,'CORE_SHA256',runner.sha(core)):
            p=runner.plan(self.save(c))
        with patch.object(runner.subprocess,'run',side_effect=subprocess.CalledProcessError(2,['calculation'],stderr='bad sample')):
            with self.assertRaises(runner.ArrivalExecutionError):runner.execute(p)
        attempt=json.loads((Path(c['output'])/'attempt.json').read_text(encoding='utf-8'))
        self.assertEqual(attempt['state'],'FAILED')
        self.assertEqual(attempt['stderr'],'bad sample')
        self.assertFalse((Path(c['output'])/'arrival-accuracy.xlsx').exists())


if __name__=='__main__':
    unittest.main()
