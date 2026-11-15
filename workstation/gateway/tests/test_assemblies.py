"""Local archive catalog tests; all remote artifacts and receipts are synthetic."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway.assemblies import assembly_assets,list_assemblies,record_success,resolve_assembly,selection_inputs
from bench_gateway.core import GatewayError,Store


class AssemblyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'state')
        self.cfg={'remote_users_root':'/data/users'};self.user='ou_assembly_alice';self.other='ou_assembly_bob'
        for user in (self.user,self.other):self.store.allow(user)
        self.sequence=0;self.job,self.result=self.example()

    def example(self,user=None,name='软件包A',software='f'*64,digest='3'*64):
        user=user or self.user;owner=self.store.remote_root(user,self.cfg['remote_users_root'])
        package={'id':'raw-app','owner':user,'name':'raw-package','kind':'package','version':1,
                 'data':{'remote_path':owner+'/package/source.app.tar.gz','sha256':'b'*64}}
        roles=(('lib/libxcamera.so','c'),('lib/libxlidar.so','d'),('lib/liburoad_m100.so','e'))
        dependencies=[{'id':'raw-'+letter,'owner':user,'name':'raw-'+letter,'kind':'dependency','version':1,
                       'data':{'target':target,'sha256':letter*64,'remote_path':owner+'/dependency/'+target.split('/')[-1]}}
                      for target,letter in roles]
        self.sequence+=1
        payload={'action':'replay','assets':{'package':package,'dependencies':dependencies},
                 'baseline':{'data':{'kind':'candidate-v1','binding':{'input_digest':digest}}},
                 'business_contract':{'software_name':name}}
        job,_=self.store.submit(user,'feishu:oc_assembly:'+user,'synthetic-source-'+str(self.sequence),payload)
        with self.store.connect() as connection:connection.execute("UPDATE jobs SET state='RUNNING' WHERE id=?",(job['id'],))
        job=self.store.job(user,job['id']);scope=owner+'/assemblies/'+software
        archive={'schema_version':1,'kind':'saved-assembly-v1','state':'SAVED','owner_root':owner,
                 'source_run':job['id'],'input_digest':digest,'software_identity':software,'package_source_sha256':'b'*64,
                 'artifact':{'path':scope+'/assembled.app.tar.gz','sha256':'a'*64,'bytes':1234},
                 'dependencies':[{'target':target,'path':scope+'/dependencies/'+target.split('/')[-1],
                                  'sha256':letter*64,'bytes':100} for target,letter in roles],
                 'manifest_path':scope+'/manifest.json','manifest_sha256':'1'*64,
                 'evidence_path':scope+'/evidence/'+job['id']+'.json','evidence_sha256':'2'*64,
                 'configuration_policy':'original-package-defaults','requires_current_precheck':True,'hardware_actions':'NOT_RUN'}
        result={'state':'PASS','run_id':job['id'],'input_digest':digest,'task':'/workstation/tasks/'+job['id'],
                'smoke':{'state':'PASS','run_id':job['id']+'-smoke','input_digest':digest},'assembly_archive':archive}
        return job,result

    def save(self,job=None,result=None):return record_success(self.cfg,self.store,job or self.job,result or self.result)

    def record(self):
        metadata=self.save()
        return resolve_assembly(self.cfg,self.store,self.user,metadata['assembly']['id'])

    def update_payload(self,job,callback):
        job=copy.deepcopy(job);callback(job['payload'])
        with self.store.connect() as connection:
            connection.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(job['payload']),job['id']))
        return job

    def test_real_success_registers_a_distinct_local_catalog_without_raw_aliases(self):
        with patch('bench_gateway.transfer.SSH') as ssh:
            metadata=self.save();ssh.assert_not_called()
        self.assertEqual(metadata['assembly']['state'],'SAVED')
        self.assertEqual(metadata['assembly']['name'],'软件包A')
        self.assertTrue(metadata['assembly']['new_task_precheck_required'])
        record=resolve_assembly(self.cfg,self.store,self.user,'软件包A')
        self.assertEqual(record['id'],'asm-'+'f'*24)
        self.assertEqual(record['version'],1);self.assertEqual(record['origin'],'assembly_catalog')
        self.assertEqual(len(record['sources']),1)
        self.assertEqual(self.store.list_assets(self.user),[])

    def test_four_assets_preserve_embedded_dependency_bytes_and_roles(self):
        record=self.record();assets=assembly_assets(record)
        self.assertEqual(set(assets),{'package','camera_so','lidar_so','m100_so'})
        self.assertEqual(assets['package']['data']['sha256'],'a'*64)
        self.assertEqual(assets['package']['data']['package_source_sha256'],'b'*64)
        for role,sha,target in (('camera_so','c','libxcamera.so'),('lidar_so','d','libxlidar.so'),('m100_so','e','liburoad_m100.so')):
            self.assertEqual(assets[role]['data']['sha256'],sha*64)
            self.assertEqual(assets[role]['data']['target'],'lib/'+target)
            self.assertEqual(assets[role]['kind'],'dependency')
        for role,asset in assets.items():
            self.assertEqual(asset['owner'],self.user);self.assertEqual(asset['origin'],'assembly_catalog')
            self.assertEqual(asset['id'],record['id']+'-'+role)

    def test_selection_freezes_ids_and_does_not_reuse_old_yaml_or_dataset(self):
        record=self.record();inputs=selection_inputs(record)
        self.assertEqual(inputs['assembly_ref'],record['id']);self.assertEqual(inputs['software_name'],'软件包A')
        self.assertEqual(inputs['m100_mode'],'provided')
        for role in ('package','camera_so','lidar_so','m100_so'):self.assertEqual(inputs[role],record['id']+'-'+role)
        for field in ('dataset','scene_scope','yaml_request','goal','lpai_target'):self.assertNotIn(field,inputs)

    def test_same_source_receipt_is_idempotent_and_reopened_catalog_agrees(self):
        first=self.save();second=self.save()
        self.assertEqual(first,second)
        reopened=Store(self.store.root)
        records=list_assemblies(self.cfg,reopened,self.user)
        self.assertEqual(len(records),1);self.assertEqual(len(records[0]['sources']),1)

    def test_new_success_run_keeps_software_id_and_preserves_each_source_evidence(self):
        first=self.save();job,result=self.example(digest='4'*64)
        second=self.save(job,result)
        self.assertEqual(first['assembly']['id'],second['assembly']['id'])
        record=resolve_assembly(self.cfg,self.store,self.user,first['assembly']['id'])
        self.assertEqual(len(record['sources']),2)
        self.assertEqual({source['source_job'] for source in record['sources']},{self.job['id'],job['id']})
        self.assertEqual(record['data']['evidence_path'],self.result['assembly_archive']['evidence_path'])
        self.assertEqual(record['version'],1)

    def test_reused_assembly_preserves_original_ccu_lineage_and_records_actual_input(self):
        first=self.save();job,result=self.example(digest='4'*64)
        def reuse(payload):
            package=payload['assets']['package'];package['origin']='assembly_catalog'
            package['data']['sha256']='a'*64;package['data']['package_source_sha256']='b'*64
        job=self.update_payload(job,reuse)
        second=self.save(job,result)
        self.assertEqual(first['assembly']['id'],second['assembly']['id'])
        record=resolve_assembly(self.cfg,self.store,self.user,first['assembly']['id'])
        self.assertEqual({source['package_source_sha256'] for source in record['sources']},{'b'*64})
        self.assertEqual({source['input_package_sha256'] for source in record['sources']},{'a'*64,'b'*64})

    def test_raw_asset_cannot_override_ccu_lineage_with_an_assembly_only_field(self):
        job=self.update_payload(self.job,lambda p:p['assets']['package']['data'].update(sha256='a'*64,package_source_sha256='b'*64))
        with self.assertRaises(GatewayError):self.save(job=job)

    def test_multiple_versions_require_an_exact_id_without_latest_drift(self):
        first=self.save();old=resolve_assembly(self.cfg,self.store,self.user,'软件包A')
        job,result=self.example(software='9'*64)
        result['assembly_archive']['artifact']['sha256']='8'*64
        second=self.save(job,result)
        self.assertNotEqual(first['assembly']['id'],second['assembly']['id'])
        self.assertEqual(second['assembly']['version'],2)
        with self.assertRaisesRegex(GatewayError,'多个已保存版本'):resolve_assembly(self.cfg,self.store,self.user,'软件包A')
        self.assertEqual(resolve_assembly(self.cfg,self.store,self.user,first['assembly']['id']),old)

    def test_catalog_is_owner_scoped_even_when_software_identity_is_the_same(self):
        first=self.save()
        self.assertEqual(list_assemblies(self.cfg,self.store,self.other),[])
        with self.assertRaises(GatewayError):resolve_assembly(self.cfg,self.store,self.other,first['assembly']['id'])
        job,result=self.example(user=self.other,name='Bob版本')
        second=self.save(job,result)
        self.assertEqual(first['assembly']['id'],second['assembly']['id'])
        self.assertEqual(resolve_assembly(self.cfg,self.store,self.user,first['assembly']['id'])['name'],'软件包A')
        self.assertEqual(resolve_assembly(self.cfg,self.store,self.other,second['assembly']['id'])['name'],'Bob版本')

    def test_same_software_id_cannot_replace_artifact_or_manifest(self):
        self.save();job,result=self.example()
        for field in ('artifact','manifest'):
            changed=copy.deepcopy(result)
            if field=='artifact':changed['assembly_archive']['artifact']['sha256']='7'*64
            else:changed['assembly_archive']['manifest_sha256']='7'*64
            with self.subTest(field=field),self.assertRaisesRegex(GatewayError,'不同的工件或清单'):self.save(job,changed)
        self.assertEqual(len(list_assemblies(self.cfg,self.store,self.user)[0]['sources']),1)

    def test_same_source_cannot_replace_already_saved_evidence(self):
        self.save();changed=copy.deepcopy(self.result)
        changed['assembly_archive']['evidence_sha256']='7'*64
        with self.assertRaisesRegex(GatewayError,'拒绝覆盖成功证据'):self.save(result=changed)
        self.assertEqual(list_assemblies(self.cfg,self.store,self.user)[0]['sources'][0]['evidence_sha256'],'2'*64)

    def test_same_software_uses_first_canonical_name_and_records_new_submitted_name(self):
        self.save();job,result=self.example(name='改名请求')
        metadata=self.save(job,result)
        self.assertEqual(metadata['assembly']['name'],'软件包A')
        record=resolve_assembly(self.cfg,self.store,self.user,metadata['assembly']['id'])
        self.assertIn('改名请求',[source['submitted_name'] for source in record['sources']])

    def test_evaluation_failure_does_not_remove_replay_success_eligibility(self):
        self.store.finish(self.job,'PARTIAL_SUCCESS',{'error':'evaluation failed','task':self.result['task']})
        changed={**self.result,'evaluation_state':'FAILED','error':'evaluation failed'}
        self.assertEqual(self.save(result=changed)['assembly']['state'],'SAVED')

    def test_recovered_success_can_archive_without_rerunning_an_unknown_task(self):
        self.store.finish(self.job,'NEEDS_REVIEW',{'error':'SSH disconnected'})
        job=self.store.job(self.user,self.job['id'])
        self.assertEqual(self.save(job=job)['assembly']['state'],'SAVED')

    def test_simulated_and_static_receipts_are_never_archived(self):
        cases=({'simulated':True},{'state':'SIMULATED'},{'state':'STATIC_PASSED'},
               {'assembly_archive':{**self.result['assembly_archive'],'state':'STATIC_PASSED'}})
        for update in cases:
            with self.subTest(update=list(update)),self.assertRaises(GatewayError):self.save(result={**self.result,**update})
        self.assertEqual(list_assemblies(self.cfg,self.store,self.user),[])

    def test_retained_result_or_legacy_raw_baseline_cannot_claim_assembly_success(self):
        job=self.update_payload(self.job,lambda p:p.update(retained_result=True))
        with self.assertRaises(GatewayError):self.save(job=job)
        job=self.update_payload(job,lambda p:(p.pop('retained_result'),p['baseline']['data'].update(kind='legacy')))
        with self.assertRaises(GatewayError):self.save(job=job)

    def test_full_smoke_and_archive_must_match_current_input_digest_and_run(self):
        for change in ('full_digest','full_run','smoke_missing','smoke_run','smoke_digest','archive_run','archive_digest'):
            result=copy.deepcopy(self.result)
            if change=='full_digest':result['input_digest']='0'*64
            elif change=='full_run':result['run_id']='other'
            elif change=='smoke_missing':result.pop('smoke')
            elif change=='smoke_run':result['smoke']['run_id']='old-smoke'
            elif change=='smoke_digest':result['smoke']['input_digest']='0'*64
            elif change=='archive_run':result['assembly_archive']['source_run']='old-run'
            else:result['assembly_archive']['input_digest']='0'*64
            with self.subTest(change=change),self.assertRaises(GatewayError):self.save(result=result)

    def test_dependency_inventory_cannot_be_missing_swapped_or_changed(self):
        for change in ('missing','duplicate','wrong_hash','wrong_target'):
            result=copy.deepcopy(self.result);items=result['assembly_archive']['dependencies']
            if change=='missing':items.pop()
            elif change=='duplicate':items[1]=copy.deepcopy(items[0])
            elif change=='wrong_hash':items[0]['sha256']='0'*64
            else:items[0]['target']='lib/other.so'
            with self.subTest(change=change),self.assertRaises(GatewayError):self.save(result=result)

    def test_package_source_and_owner_cannot_be_changed(self):
        result=copy.deepcopy(self.result);result['assembly_archive']['package_source_sha256']='0'*64
        with self.assertRaises(GatewayError):self.save(result=result)
        job=self.update_payload(self.job,lambda p:p['assets']['dependencies'][0].update(owner=self.other))
        with self.assertRaises(GatewayError):self.save(job=job)

    def test_all_archive_paths_stay_inside_the_owner_and_software_identity(self):
        foreign=self.store.remote_root(self.other,self.cfg['remote_users_root'])
        for change in ('foreign_owner','foreign_artifact','wrong_identity','traversal','relative','repeated_path'):
            result=copy.deepcopy(self.result);archive=result['assembly_archive']
            if change=='foreign_owner':archive['owner_root']=foreign
            elif change=='foreign_artifact':archive['artifact']['path']=foreign+'/assemblies/'+archive['software_identity']+'/assembled.app.tar.gz'
            elif change=='wrong_identity':archive['manifest_path']=archive['owner_root']+'/assemblies/'+'0'*64+'/manifest.json'
            elif change=='traversal':archive['evidence_path']=archive['owner_root']+'/assemblies/'+archive['software_identity']+'/../escape.json'
            elif change=='relative':archive['artifact']['path']='relative.tgz'
            else:archive['evidence_path']=archive['artifact']['path']
            with self.subTest(change=change),self.assertRaises(GatewayError):self.save(result=result)

    def test_archive_requires_nonempty_bytes_and_valid_lowercase_hashes(self):
        for value in (0,-1,True,'100'):
            result=copy.deepcopy(self.result);result['assembly_archive']['artifact']['bytes']=value
            with self.subTest(bytes=value),self.assertRaises(GatewayError):self.save(result=result)
        result=copy.deepcopy(self.result);result['assembly_archive']['artifact']['sha256']='A'*64
        with self.assertRaises(GatewayError):self.save(result=result)

    def test_archive_does_not_save_task_specific_yaml_or_waive_future_gate(self):
        for update in ({'configuration_policy':'last-run-effective-yaml'},{'requires_current_precheck':False}):
            with self.subTest(update=update),self.assertRaises(GatewayError):
                self.save(result={**self.result,'assembly_archive':{**self.result['assembly_archive'],**update}})

    def test_submitted_job_payload_and_saved_user_permissions_are_authoritative(self):
        job=copy.deepcopy(self.job);job['payload']['business_contract']['software_name']='forged'
        with self.assertRaises(GatewayError):self.save(job=job)
        self.save()
        with self.store.connect() as connection:connection.execute('UPDATE users SET enabled=0 WHERE id=?',(self.user,))
        with self.assertRaises(GatewayError):list_assemblies(self.cfg,self.store,self.user)

    def test_raw_package_name_is_a_valid_fallback_and_returned_records_are_detached(self):
        job=self.update_payload(self.job,lambda p:p['business_contract'].update(software_name=''))
        metadata=self.save(job=job)
        self.assertEqual(metadata['assembly']['name'],'raw-package')
        record=resolve_assembly(self.cfg,self.store,self.user,'raw-package');record['data']['artifact']['sha256']='0'*64
        self.assertEqual(resolve_assembly(self.cfg,self.store,self.user,'raw-package')['data']['artifact']['sha256'],'a'*64)


if __name__=='__main__':unittest.main()
