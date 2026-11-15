from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from bench_gateway.login_startup import run


class LoginStartupTests(unittest.TestCase):
    def test_existing_supervisor_is_not_restarted(self):
        with TemporaryDirectory() as root,patch('bench_gateway.login_startup.status',return_value={'active':True,'instance':'existing'}),patch('bench_gateway.login_startup.start') as start:
            self.assertEqual(run({'project_root':root})['state'],'ALREADY_RUNNING')
            start.assert_not_called()

    def test_stale_entry_starts_once_and_requires_both_components_ready(self):
        samples=[{'active':False,'phase':'STALE'}, {'active':True,'phase':'STARTING'},
                 {'active':True,'phase':'RUNNING','instance':'new','children':{'bridge':{'ready':True},'worker':{'ready':True}}}]
        with TemporaryDirectory() as root,patch('bench_gateway.login_startup.status',side_effect=samples),patch('bench_gateway.login_startup.start') as start,patch('bench_gateway.login_startup.time.sleep'):
            self.assertEqual(run({'project_root':root})['state'],'RUNNING')
            start.assert_called_once()
            self.assertTrue((Path(root)/'runtime/service/login-start.json').is_file())

    def test_timeout_is_not_marked_as_success_or_retried(self):
        with TemporaryDirectory() as root,patch('bench_gateway.login_startup.status',return_value={'active':False}),patch('bench_gateway.login_startup.start') as start:
            self.assertEqual(run({'project_root':root},wait_seconds=0)['state'],'NOT_READY')
            start.assert_called_once()


if __name__=='__main__':unittest.main()
