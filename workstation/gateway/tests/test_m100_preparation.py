import base64
import json
import unittest
from unittest.mock import patch

from bench_gateway.intake import Intake
from bench_gateway.preparation import MaterialCheck
import test_preparation_progress as progress_fixture


class BuildPreparationTests(unittest.TestCase):
    def fixture(self):
        helper=progress_fixture.PreparationProgressTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.running(m100_mode='build_if_missing',m100_so='')
        service,fetch=helper.material_service(verify_phases=False)
        return helper,service,fetch

    def test_build_resolves_missing_m100_without_mutating_submitted_draft(self):
        h,service,fetch=self.fixture();requests=[]
        def inspect(code,args,**kwargs):
            request=json.loads(base64.b64decode(args[0]));requests.append(request)
            return json.dumps({'schema_version':1,'status':'PASS' if request['m100_so'] else 'NEEDS_INPUT',
                'issues':[] if request['m100_so'] else [{'field':'m100_so','code':'M100_BUILD_REQUIRED'}],'checks':[]})
        service.registry.ssh.python=inspect
        with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.asset_fetch.fetch',side_effect=fetch), \
             patch('bench_gateway.m100_build.build',return_value=(h.asset('m100_so'),{'state':'BUILT'})) as build:
            result=MaterialCheck(h.cfg,h.store).run(h.job)
        self.assertEqual('STATIC_PASSED',result['status']);self.assertEqual(2,len(requests))
        self.assertIsNone(requests[0]['m100_so']);self.assertEqual('a'*64,requests[1]['m100_so']['sha256'])
        self.assertFalse(Intake(h.store).get(h.user).get('m100_so'));build.assert_called_once()
        self.assertEqual('BUILT',result['m100_build']['state'])

    def test_known_input_error_skips_compilation_and_yaml_model(self):
        h,service,fetch=self.fixture()
        service.registry.ssh.python=lambda *a,**kw:json.dumps({'schema_version':1,'status':'NEEDS_INPUT',
            'checks':[],'issues':[{'field':'lidar_so','code':'ELF_TARGET_MISMATCH'}]})
        with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.asset_fetch.fetch',side_effect=fetch), \
             patch('bench_gateway.m100_build.build') as build,patch('bench_gateway.yaml_plan.plan') as model:
            result=MaterialCheck(h.cfg,h.store).run(h.job)
        self.assertEqual('NEEDS_INPUT',result['status']);build.assert_not_called();model.assert_not_called()

    def test_missing_build_profile_is_not_reported_as_old_so_validation_error(self):
        from bench_gateway.m100_build import BuildError
        h,service,fetch=self.fixture()
        service.registry.ssh.python=lambda *a,**kw:json.dumps({'schema_version':1,'status':'NEEDS_INPUT','checks':[{'field':'m100_so','check':'M100_BUILD_REQUIRED','status':'FAIL'}],'issues':[{'field':'m100_so','code':'M100_BUILD_REQUIRED'}]})
        with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.asset_fetch.fetch',side_effect=fetch),patch('bench_gateway.m100_build.reusable',side_effect=AssertionError('historical M100 must not be selected')),patch('bench_gateway.m100_build.build',side_effect=BuildError('M100_RECIPE_MISMATCH','缺少本APP源码','补充配套源码')):
            result=MaterialCheck(h.cfg,h.store).run(h.job)
        self.assertEqual('NEEDS_CONFIGURATION',result['m100_build']['state'])
        self.assertFalse(result['m100_build']['old_artifact_reused'])
        self.assertNotIn('M100_BUILD_REQUIRED',[x.get('check') for x in result['checks']])

    def test_upstream_error_defers_build_without_false_m100_validation_error(self):
        h,service,fetch=self.fixture()
        service.registry.ssh.python=lambda *a,**kw:json.dumps({'schema_version':1,'status':'NEEDS_INPUT',
            'checks':[{'field':'m100_so','check':'M100_BUILD_REQUIRED','status':'FAIL'}],
            'issues':[{'field':'lidar_so','code':'ELF_TARGET_MISMATCH'},{'field':'m100_so','code':'M100_BUILD_REQUIRED'}]})
        with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.asset_fetch.fetch',side_effect=fetch),patch('bench_gateway.m100_build.build') as build:
            result=MaterialCheck(h.cfg,h.store).run(h.job)
        build.assert_not_called()
        self.assertEqual('NEEDS_INPUT',result['status'])
        self.assertEqual('WAITING_FOR_MATERIALS',result['m100_build']['state'])
        self.assertNotIn('M100_BUILD_REQUIRED',[i.get('code') for i in result['issues']])
        self.assertIn('ELF_TARGET_MISMATCH',[i.get('code') for i in result['issues']])