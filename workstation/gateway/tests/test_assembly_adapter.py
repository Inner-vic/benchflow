"""Archive adapter integration with tiny real files and a mocked Runner boundary.

No SSH, process, installer, cleanup operation or bench method is permitted here.
The fixed local validate-and-retain boundary represents Runner's verification;
the gateway still rechecks both retained plans, binding and cleanup receipts.
"""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import unittest
from unittest.mock import Mock,patch

from bench_gateway import assembly_remote,candidate_files,candidate_remote,remote_agent
import test_candidate_remote as fixtures


class AssemblyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.CandidateRemoteTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        c=self.case
        # The generic candidate fixture uses owner_root itself as dataset_root;
        # the real remote private-source adapter requires a strict descendant.
        dataset=c.root/'dataset';dataset.mkdir()
        moved_manifest=dataset/'manifest.json';shutil.move(c.manifest,moved_manifest);c.manifest=moved_manifest
        cameras=[]
        for raw in c.assets['dataset']['data']['camera_roots']:
            source=Path(raw);target=dataset/source.name;shutil.move(source,target);cameras.append(str(target))
        c.assets['dataset']['data'].update(remote_path=str(dataset),manifest=str(c.manifest),camera_roots=cameras)
        self.request=c.bound_request(operation='archive_assembly',run_id='job-now',scenes=[1,2],
                                     smoke_run='job-now-smoke',smoke_scenes=[1])
        self.plans={run:c.materialize(self.request,run,indices) for run,indices in
                    (('job-now',[1,2]),('job-now-smoke',[1]))}
        self.products={}
        for run,indices in (('job-now',[1,2]),('job-now-smoke',[1])):
            task=c.m.ROOT/'tasks'/run;task.mkdir(parents=True)
            logs=task/'logs';logs.mkdir()
            run_root=c.m.ROOT/'runs'/run;run_root.mkdir(parents=True)
            product=c.m.ROOT/'cache/predictions'/run/'predictions';product.mkdir(parents=True)
            files=[]
            for number in indices:
                path=product/('scene-'+('one' if number==1 else 'two'))/'pred_traj.txt'
                path.parent.mkdir();path.write_bytes(b'synthetic prediction only')
                files.append({'path':path.relative_to(product).as_posix(),'size':path.stat().st_size,'sha256':c.sha(path)})
            inventory=hashlib.sha256(candidate_remote.canonical(files)).hexdigest()
            identity=hashlib.sha256(candidate_remote.canonical({'producer':run,'files':files})).hexdigest()
            receipt=product.with_name('predictions.bench-eval-product.json')
            producer={'state':'SUCCEEDED','producer_run_id':run,'product_type':'workstation_predictions',
                      'source':'/app/livc-test/'+run+'/predictions','workstation_product':str(product),
                      'inventory_sha256':inventory,'product_identity':identity,'files':files}
            self.write(receipt,producer)
            self.write(run_root/'state.json',{'state':'COMPLETED_TO_REQUESTED_GOAL','run_id':run})
            self.write(run_root/'restore-verification.json',{'restore_ok':True,'run_id':run})
            self.write(task/'index.json',{'state':'PASS','task_id':run,'run_id':run,
                      'inventory_sha256':inventory,'product_identity':identity})
            cleanup=c.m.ROOT/'maintenance'/(run+'-cleanup.json')
            self.write(cleanup,{'batch_id':run+'-cleanup','accepted':[{'run_id':run,
                'source':producer['source'],'workstation_product':str(product),'product_receipt':str(receipt),
                'inventory_sha256':inventory}],'removed':[producer['source']]})
            shutil.copyfile(cleanup,logs/'bench-output-cleanup.json')
            self.products[run]=(task,receipt)
        def retained(plan,code):
            self.assertEqual(code,0)
            self.assertEqual(plan,{**self.plans[plan['run_id']],'resume_delivery':True})
            return self.products[plan['run_id']]
        c.m.validate_and_retain=Mock(side_effect=retained)
        for name in ('execute','finish_delivery','bench_capacity','command'):
            setattr(c.m,name,Mock(side_effect=AssertionError('archive may not invoke '+name)))
        raw=Path(assembly_remote.__file__).read_bytes()
        self.request.update(assembly_code=base64.b64encode(raw).decode(),
                            assembly_code_sha256=hashlib.sha256(raw).hexdigest())
        self.modules={'candidate_remote':candidate_remote,'candidate_files':candidate_files,'candidate_adapter':c.adapter}

    @staticmethod
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')

    def archive(self,request=None):
        with patch('subprocess.run',side_effect=AssertionError('no process allowed')), \
             patch.object(remote_agent,'formal_matches',side_effect=AssertionError('no bench probe allowed')), \
             patch.object(remote_agent,'runtime_installer',side_effect=AssertionError('no installer allowed')):
            return remote_agent.archive_assembly(request or self.request,self.case.m,self.modules)

    def assert_no_hardware(self):
        for name in ('execute','finish_delivery','bench_capacity','command'):getattr(self.case.m,name).assert_not_called()

    def test_actual_binding_and_both_retained_runs_produce_a_real_archive(self):
        c=self.case;before=c.package.read_bytes();result=self.archive()
        self.assertEqual(result['state'],'SAVED');self.assertEqual(result['source_run'],'job-now')
        self.assertEqual(result['input_digest'],self.request['baseline']['binding']['input_digest'])
        self.assertTrue(Path(result['artifact']['path']).is_file())
        proof=json.loads(Path(result['evidence_path']).read_text(encoding='utf-8'))
        roles={entry['role'] for entry in proof['files']}
        self.assertTrue(set(assembly_remote.REQUIRED_EVIDENCE)<=roles)
        self.assertEqual(len(assembly_remote.REQUIRED_EVIDENCE),8)
        self.assertTrue({'cleanup','smoke_cleanup'}<=roles)
        binding=next(entry['snapshot'] for entry in proof['files'] if entry['role']=='binding')
        self.assertEqual(binding['frozen']['input_digest'],result['input_digest'])
        self.assertEqual(binding['binding_identity']['input_digest'],result['input_digest'])
        self.assertEqual(c.package.read_bytes(),before)
        self.assertEqual(c.m.validate_and_retain.call_count,2)
        self.assertEqual([call.args[0]['run_id'] for call in c.m.validate_and_retain.call_args_list],['job-now','job-now-smoke'])
        self.assertEqual(assembly_remote.verify(result,str(c.root)),result)
        self.assert_no_hardware()

    def test_missing_cleanup_prevents_archive_without_replay_or_recovery(self):
        (self.case.m.ROOT/'maintenance/job-now-cleanup.json').unlink()
        with self.assertRaises((OSError,ValueError)):self.archive()
        self.assertFalse((self.case.root/'assemblies').exists())
        self.assert_no_hardware()

    def test_archive_verification_dispatch_needs_no_candidate_or_runner(self):
        receipt=self.archive()
        request={key:self.request[key] for key in ('owner_root','run_id','assembly_code','assembly_code_sha256')}
        request.update(operation='verify_assembly',assembly_receipt=receipt)
        with patch.object(remote_agent,'batch_module',side_effect=AssertionError('no Runner loading')), \
             patch.object(remote_agent,'candidate_modules',side_effect=AssertionError('no candidate loading')), \
             patch('subprocess.run',side_effect=AssertionError('no process')):
            self.assertEqual(remote_agent.main(request),receipt)
            for changes in ({'run_id':'another-job'},{'owner_root':str(self.case.root/'another-owner')},
                            {'assembly_code_sha256':'0'*64}):
                with self.subTest(changes=changes),self.assertRaises((ValueError,OSError)):
                    remote_agent.main({**request,**changes})
        self.assert_no_hardware()

    def test_wrong_cleanup_or_false_restore_prevents_promotion(self):
        cleanup=self.case.m.ROOT/'maintenance/job-now-cleanup.json'
        self.write(cleanup,{'batch_id':'job-now-cleanup','accepted':[],'removed':[]})
        shutil.copyfile(cleanup,self.case.m.ROOT/'tasks/job-now/logs/bench-output-cleanup.json')
        with self.assertRaisesRegex(ValueError,'cleanup'):self.archive()
        self.assert_no_hardware()

    def test_resume_checks_the_same_job_profile_binding_and_sources_as_replay(self):
        directory=self.case.m.BASE/'adapters/job-now'
        for name in ('job.json','profile.json','gateway-binding.json','small_stream_adapter.py','sw0058_gate_probe_driver_reference.py'):
            path=directory/name;original=path.read_bytes()
            try:
                path.write_text('{}' if name.endswith('.json') else '# changed by fixture',encoding='utf-8')
                with self.subTest(name=name),self.assertRaises(ValueError):self.archive()
            finally:path.write_bytes(original)
        self.assert_no_hardware()

    def test_scope_and_archive_implementation_cannot_be_substituted(self):
        for update in ({'scenes':[1]},{'smoke_scenes':[2]},{'smoke_run':'old-smoke'},
                       {'assembly_code_sha256':'0'*64}):
            with self.subTest(update=update),self.assertRaises(ValueError):self.archive({**self.request,**update})
        self.assert_no_hardware()

    def test_false_restore_is_rejected_even_after_fixed_retention_boundary(self):
        path=self.case.m.ROOT/'runs/job-now/restore-verification.json'
        self.write(path,{'restore_ok':False,'run_id':'job-now'})
        with self.assertRaisesRegex(ValueError,'restoration'):self.archive()
        self.assertFalse((self.case.root/'assemblies').exists())
        self.assert_no_hardware()


class EmbeddedDependencyBindingTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.CandidateRemoteTests();self.case.setUp();self.addCleanup(self.case.doCleanups)

    def rewrite_package(self,matching):
        c=self.case
        with tarfile.open(c.package,'r:gz') as archive:
            members=[(member,archive.extractfile(member).read() if member.isfile() else None) for member in archive]
        by_target={asset['data']['target']:Path(asset['data']['remote_path']).read_bytes() for asset in c.assets['dependencies']}
        with tarfile.open(c.package,'w:gz') as archive:
            for member,raw in members:
                target=member.name[5:] if member.name.startswith('livc/') else member.name
                if target in matching:raw=by_target[target];member.size=len(raw)
                archive.addfile(member,io.BytesIO(raw) if raw is not None else None)
        c.assets['package']['data']['sha256']=c.sha(c.package)

    def test_embedded_flag_requires_exact_regular_member_hash_for_each_role(self):
        c=self.case;targets={asset['data']['target'] for asset in c.assets['dependencies']}
        self.rewrite_package(targets)
        request=c.bound_request();receipt=candidate_remote.read(request['baseline']['binding']['path'])
        self.assertEqual(receipt['candidate']['embedded_dependencies'],{
            asset['data']['target']:asset['data']['sha256'] for asset in c.assets['dependencies']})
        c.m.execute.assert_not_called();c.m.bench_capacity.assert_not_called()

    def test_an_unaligned_so_is_not_marked_embedded_just_because_name_exists(self):
        c=self.case;target='lib/libxcamera.so';self.rewrite_package({target})
        request=c.bound_request();receipt=candidate_remote.read(request['baseline']['binding']['path'])
        self.assertEqual(set(receipt['candidate']['embedded_dependencies']),{target})
        c.m.execute.assert_not_called();c.m.bench_capacity.assert_not_called()


if __name__=='__main__':unittest.main()
