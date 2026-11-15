import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_gateway as fixtures
from bench_gateway.intake import CLEAR_YAML, FIELDS as INPUT_FIELDS, Intake
from bench_gateway.parser import FIELDS, SCHEMA, parse, quick


class IntakeParserTests(unittest.TestCase):
    setUp=fixtures.GatewayTests.setUp
    tearDown=fixtures.GatewayTests.tearDown

    def test_routine_material_actions_bypass_model_and_keep_checks_distinct(self):
        aliases={'检查材料':'check_materials','核验材料':'check_materials','检查一下材料。':'check_materials',
                 '查看准备状态':'preparation_status','材料检查结果':'preparation_status','清空本次材料':'new_task'}
        with patch('bench_gateway.parser.subprocess.run') as model:
            for text,action in aliases.items():
                self.assertEqual(parse(self.cfg,text,{},[]),{'action':action})
                self.assertIn(action,SCHEMA['properties']['action']['enum'])
            model.assert_not_called()

    def test_questions_and_quoted_clear_commands_are_not_deterministic_mutations(self):
        for text in ('取消 YAML 修改是什么意思？','取消 YAML 修改？','“清空本次材料”','请先解释如何检查材料'):
            self.assertIsNone(quick(text))

    def test_parser_explicit_clear_roundtrip_does_not_remove_other_draft_fields(self):
        intake=Intake(self.s)
        intake.update(fixtures.A,{'package':'owned-package','yaml_request':'a.yaml test=true','m100_so':'owned-M100.so'})
        skill=Path(self.cfg['project_root'])/'.agents/skills/bench-gateway/SKILL.md'
        skill.parent.mkdir(parents=True);skill.write_text('只输出材料意图。',encoding='utf-8')
        (Path(self.cfg['project_root'])/'runtime').mkdir()
        intent={key:'' for key in FIELDS}
        intent.update(action='prepare',dependencies=[],scenes=[],evaluate_after=False,
                      inputs={key:'' for key in INPUT_FIELDS})
        intent['inputs']['yaml_request']=CLEAR_YAML

        def respond(argv,**kwargs):
            Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps(intent),encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout='')

        with patch('bench_gateway.parser.subprocess.run',side_effect=respond):
            parsed=parse(self.cfg,'刚才的 YAML 修改撤销，保留其他材料',self.service.context(fixtures.A),[])
        self.service.handle(fixtures.SESSION,'刚才的 YAML 修改撤销，保留其他材料',parsed)
        data=intake.get(fixtures.A)
        self.assertNotIn('yaml_request',data)
        self.assertEqual(data['package'],'owned-package');self.assertEqual(data['m100_so'],'owned-M100.so')
        self.assertEqual(self.s.jobs(fixtures.A),[])

    def test_execution_with_new_yaml_is_prepared_without_losing_requested_change(self):
        skill=Path(self.cfg['project_root'])/'.agents/skills/bench-gateway/SKILL.md'
        skill.parent.mkdir(parents=True);skill.write_text('只输出材料意图。',encoding='utf-8')
        (Path(self.cfg['project_root'])/'runtime').mkdir()
        request='用已登记 A 回灌，并把 common_param.yaml 的 debug.enable 改为 true'
        intent={key:'' for key in FIELDS}
        intent.update(action='prepare',dependencies=[],scenes=[],evaluate_after=False,
                      inputs={key:'' for key in INPUT_FIELDS})
        intent['inputs'].update(package='A',yaml_request='把 common_param.yaml 的 debug.enable 改为 true')

        def respond(argv,**kwargs):
            self.assertIn('不得丢弃新增要求后直接 replay',kwargs['input'])
            self.assertIn('inputs.yaml_request 必须完整携带本条要求',kwargs['input'])
            Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps(intent),encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout='')

        with patch('bench_gateway.parser.subprocess.run',side_effect=respond):
            parsed=parse(self.cfg,request,self.service.context(fixtures.A),[])
        self.service.handle(fixtures.SESSION,request,parsed)
        self.assertEqual(Intake(self.s).get(fixtures.A)['yaml_request'],intent['inputs']['yaml_request'])
        self.assertEqual(Intake(self.s).get(fixtures.A)['package'],'A')
        self.assertEqual(self.s.jobs(fixtures.A),[])

    def test_current_candidate_replay_preserves_bound_versions_without_gate_true(self):
        from bench_gateway.bindings import bind_preparation, implementation_snapshot
        from bench_gateway.preparation import PreparationQueue
        # The transport is mocked; identity verification still hashes a real,
        # isolated copy of the installed administrator sources and provenance.
        source=Path(__file__).resolve().parents[1]
        for name in ('bench_gateway/candidate_files.py','bench_gateway/candidate_adapter.py',
                     'bench_gateway/candidate_remote.py','vendor/run_stream_batch.py','vendor/provenance.json'):
            target=Path(self.cfg['project_root'])/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((source/name).read_bytes())
        self.cfg.update(default_adapter_template='reviewed',adapter_templates={
            'reviewed':{'template_id':'tpl-test','source_run':'reviewed-run'}})
        package=self.s.add_asset(fixtures.A,'package','A',{'sha256':'a'*64})
        dataset=self.s.add_asset(fixtures.A,'dataset','D',{'sha256':'b'*64,'scene_count':2})
        dependencies={field:self.s.add_asset(fixtures.A,'dependency',field,{'sha256':str(index)*64})
                      for index,field in enumerate(('camera_so','lidar_so','m100_so'),1)}
        Intake(self.s).update(fixtures.A,{'goal':'replay','package':'A','dataset':'D','scene_scope':'1,2',
                                       **{field:field for field in dependencies}},new=True)
        queue=PreparationQueue(self.cfg,self.s);queue.submit(fixtures.A,fixtures.SESSION)
        preparation=queue.latest(fixtures.A)
        result={'status':'STATIC_PASSED','assets':{'package':package,**dependencies},
                'dataset':dataset,'scenes':[1,2],'yaml_preparation':{}}
        with self.s.connect() as c:
            c.execute("UPDATE preparations SET state='STATIC_PASSED',result=? WHERE id=?",
                      (json.dumps(result),preparation['id']))
        receipt={'state':'READY_FOR_GATE','path':self.s.remote_root(fixtures.A,'/data/users')+'/binding.json',
                 'sha256':'c'*64,'input_digest':'d'*64,'template_id':'tpl-test',
                 'implementation_hashes':implementation_snapshot(self.cfg['project_root'])}
        with patch('bench_gateway.executor.Executor.bind_candidate',return_value=receipt,create=True):
            baseline=bind_preparation(self.cfg,self.s,fixtures.A)
        skill=Path(self.cfg['project_root'])/'.agents/skills/bench-gateway/SKILL.md'
        skill.parent.mkdir(parents=True);skill.write_text('只输出材料意图。',encoding='utf-8')
        (Path(self.cfg['project_root'])/'runtime').mkdir()
        intent={key:'' for key in FIELDS}
        intent.update(action='replay',baseline=baseline['name'],dependencies=[],scenes=[],evaluate_after=False,
                      inputs={key:'' for key in INPUT_FIELDS})
        context=self.service.context(fixtures.A)
        summary=next(item for item in context['baselines'] if item['name']==baseline['name'])
        self.assertTrue(summary['current']);self.assertIs(summary['gate_verified'],False)

        def respond(argv,**kwargs):
            Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps(intent),encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout='')

        with patch('bench_gateway.parser.subprocess.run',side_effect=respond):
            parsed=parse(self.cfg,'执行回灌',context,[])
        self.service.handle(fixtures.SESSION,'执行回灌',parsed)
        job=self.s.jobs(fixtures.A)[0]
        self.assertEqual(job['payload']['assets']['package']['id'],package['id'])
        self.assertEqual(job['payload']['scenes'],[1,2])
        self.assertIs(job['payload']['baseline']['data']['gate_verified'],False)


if __name__=='__main__':unittest.main()
