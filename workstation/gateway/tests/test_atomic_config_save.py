"""Status readers and overlapping writes must not strand the supervisor."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway import config


class AtomicSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.path=self.root/'status.json'
        config.save(self.path,{'phase':'OLD'})

    def tearDown(self):self.temp.cleanup()

    def sharing_error(self,code=32):
        error=PermissionError('reader temporarily holds the status file')
        error.winerror=code
        return error

    def test_reader_conflict_is_retried_then_commits_complete_status(self):
        replace=config.os.replace
        outcomes=iter([self.sharing_error(5),self.sharing_error(32),None])
        def conflict(source,target):
            self.assertEqual(json.loads(self.path.read_text()),{'phase':'OLD'})
            error=next(outcomes)
            if error:raise error
            replace(source,target)
        with patch.object(config.os,'replace',side_effect=conflict) as mocked, \
                patch.object(config.time,'sleep') as sleep:
            config.save(self.path,{'phase':'RUNNING'})
        self.assertEqual(mocked.call_count,3)
        self.assertEqual(sleep.call_count,2)
        self.assertEqual(json.loads(self.path.read_text()),{'phase':'RUNNING'})
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_conflict_has_a_bound_and_keeps_previous_state(self):
        with patch.object(config.os,'replace',side_effect=self.sharing_error()), \
                patch.object(config.time,'sleep') as sleep:
            with self.assertRaises(PermissionError):config.save(self.path,{'phase':'RUNNING'})
        self.assertEqual(sleep.call_count,6)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list),.64)
        self.assertEqual(json.loads(self.path.read_text()),{'phase':'OLD'})
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_other_permission_errors_fail_immediately(self):
        with patch.object(config.os,'replace',side_effect=PermissionError('access denied')), \
                patch.object(config.time,'sleep') as sleep:
            with self.assertRaises(PermissionError):config.save(self.path,{'phase':'NEW'})
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()),{'phase':'OLD'})

    def test_overlapping_saves_never_steal_each_others_temporary_file(self):
        replace=config.os.replace
        paths=[]
        def overlap(source,target):
            paths.append(source)
            if len(paths)==1:config.save(self.path,{'writer':'inner'})
            replace(source,target)
        with patch.object(config.os,'replace',side_effect=overlap):
            config.save(self.path,{'writer':'outer'})
        self.assertEqual(len(set(paths)),2)
        self.assertEqual(json.loads(self.path.read_text()),{'writer':'outer'})
        self.assertEqual(list(self.root.glob('*.tmp')),[])


if __name__=='__main__':unittest.main()
