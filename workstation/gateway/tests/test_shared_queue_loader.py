"""Public plugin contract only; no task submission or board operations."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch

from bench_gateway import roadmodel_plugin,roadmodel_queue

class SharedQueueLoaderTests(TestCase):
    def test_uses_public_release_checked_loader_for_each_module(self):
        modules={name:object() for name in ('runtime','active_execution','two_link')}
        with patch.object(roadmodel_plugin,'backend',side_effect=modules.__getitem__) as loader:
            result=roadmodel_queue.modules()
        self.assertIs(result[0],roadmodel_plugin)
        self.assertEqual(tuple(modules.values()),result[1:])
        self.assertEqual([('runtime',),('active_execution',),('two_link',)],[c.args for c in loader.call_args_list])

    def test_wrong_release_error_is_not_bypassed(self):
        with patch.object(roadmodel_plugin,'backend',side_effect=ValueError('Unexpected RoadModel module release')):
            with self.assertRaisesRegex(ValueError,'Unexpected RoadModel'):roadmodel_queue.modules()

    def test_runtime_keeps_current_release_catalog_and_shared_state(self):
        root=Path('/fixed/reviewed/roadmodel');runtime=SimpleNamespace(Runtime=Mock(return_value='runtime'))
        queue=roadmodel_queue.HardwareQueue.__new__(roadmodel_queue.HardwareQueue)
        queue.cfg={'state_root':'/shared/state'}
        with patch.object(roadmodel_queue,'modules',return_value=(SimpleNamespace(ROOT=root),runtime,None,None)):
            self.assertEqual('runtime',queue.runtime())
        runtime.Runtime.assert_called_once_with(Path('/shared/state/roadmodel'),catalog_file=root/'configs/two_link_catalog.json')
