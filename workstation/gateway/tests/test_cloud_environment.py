import unittest
from unittest.mock import patch
from types import SimpleNamespace

from bench_gateway.cloud_environment import diagnose,inspect_instance


class CloudEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.profile={'instance':'bench-eval-offline','namespace':'vc-chassis','cluster':'fixed-cluster','profile_sha256':'a'*64}
        self.instance={'state':'OBSERVED','name':'bench-eval-offline','namespace':'vc-chassis','uid':'fixed-uid','phase':'ready','checked_at':100}
        self.heartbeat={'instance':'bench-eval-offline','profile_sha256':'a'*64,'state':'READY','generation':'new-generation','checked_at':100}

    def diagnosis(self,instance=None,heartbeat=None,**kwargs):
        return diagnose(self.instance if instance is None else instance,self.heartbeat if heartbeat is None else heartbeat,self.profile,now=100,**kwargs)

    def test_ready_and_busy_are_never_restarted(self):
        self.assertEqual(self.diagnosis()['state'],'READY')
        d=self.diagnosis(heartbeat={**self.heartbeat,'state':'EVALUATING'})
        self.assertTrue(d['busy']);self.assertEqual(d['browser_action'],'NONE')

    def test_only_explicit_stopped_state_requests_fixed_instance_start(self):
        for phase in ('stopped','paused'):
            d=self.diagnosis(instance={**self.instance,'phase':phase})
            self.assertEqual(d,{'state':'STOPPED','browser_action':'START_FIXED_INSTANCE'})

    def test_platform_transition_failure_or_unknown_does_not_click_start(self):
        for phase,state in [('starting','STARTING'),('creating','STARTING'),('pending','STARTING'),
                            ('stopping','STOPPING'),('failed','INSTANCE_FAILED'),('unrecognized','UNKNOWN_PLATFORM_STATE')]:
            with self.subTest(phase=phase):
                d=self.diagnosis(instance={**self.instance,'phase':phase})
                self.assertEqual(d['state'],state);self.assertEqual(d['browser_action'],'NONE')

    def test_missing_stale_malformed_or_wrong_profile_heartbeat_is_not_a_stopped_instance(self):
        for h in ({},{**self.heartbeat,'checked_at':0},{**self.heartbeat,'checked_at':'bad'},
                  {**self.heartbeat,'checked_at':float('nan')},{**self.heartbeat,'profile_sha256':'b'*64},
                  {**self.heartbeat,'instance':'someone-else'},{**self.heartbeat,'generation':''}):
            d=self.diagnosis(heartbeat=h)
            self.assertEqual(d['state'],'RECEIVER_NOT_READY');self.assertEqual(d['browser_action'],'NONE')

    def test_old_generation_cannot_complete_a_cold_start(self):
        self.assertEqual(self.diagnosis(previous_generation='new-generation')['state'],'RECEIVER_NOT_READY')
        self.assertEqual(self.diagnosis(previous_generation='old-generation')['state'],'READY')

    def test_unknown_or_stale_platform_result_never_infers_paused(self):
        for i in ({'state':'PLATFORM_UNAVAILABLE'},{**self.instance,'name':'other'},
                  {**self.instance,'phase':'stopped','checked_at':0},
                  {**self.instance,'checked_at':'bad'}):
            self.assertEqual(self.diagnosis(instance=i)['browser_action'],'NONE')

    def test_platform_identity_is_validated_without_start_or_stop_commands(self):
        import json
        body={'data':{'metadata':{'name':'wrong','namespace':'vc-chassis','uid':'x'},'status':{'phase':'stopped'}}}
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(body))) as run:
            value=inspect_instance('lpai-agent',self.profile)
        self.assertEqual(value['state'],'PLATFORM_UNAVAILABLE')
        self.assertEqual(run.call_args.args[0][1:3],['devspaces','get'])

    def test_platform_auth_or_timeout_errors_do_not_expose_raw_output(self):
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='credential-must-not-appear')):
            result=inspect_instance('lpai-agent',self.profile)
        self.assertEqual(result['state'],'PLATFORM_UNAVAILABLE')
        self.assertNotIn('credential',str(result))


if __name__=='__main__':unittest.main()
