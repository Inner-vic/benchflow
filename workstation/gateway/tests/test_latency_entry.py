import subprocess
import tempfile
from pathlib import Path
import unittest
import os
from unittest.mock import patch

from bench_gateway.parser import command, quick, parse
from bench_gateway.parser import resolve_binary
from bench_gateway.core import GatewayError
from bench_gateway.yaml_plan import plan


class LatencyTests(unittest.TestCase):
    def test_custom_cli_launcher_is_not_replaced_with_desktop_binary(self):
        with patch('bench_gateway.parser.shutil.which') as search:
            self.assertEqual('codex-test',resolve_binary({'codex_binary':'codex-test'}));search.assert_not_called()
    def test_retired_desktop_cli_path_resolves_current_installed_cli(self):
        with patch('bench_gateway.parser.shutil.which',return_value='C:/current/codex.exe'):
            self.assertEqual('C:/current/codex.exe',resolve_binary({'codex_binary':'C:/retired/codex.exe'}))

    def test_existing_explicit_cli_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            cli=Path(directory)/'codex.exe';cli.write_bytes(b'test')
            with patch('bench_gateway.parser.shutil.which') as search:
                self.assertEqual(str(cli),resolve_binary({'codex_binary':str(cli)}));search.assert_not_called()

    def test_exact_yaml_attachment_replacement_bypasses_model_and_still_validates_paths(self):
        with patch('bench_gateway.yaml_plan.subprocess.run') as model:
            result=plan({},'用工作站文件 /data/users/u_a/inbox/a.yaml 替换 APP 内 etc/uroad/config/common_param.yaml')
            self.assertEqual('READY',result['status']);self.assertEqual('replace',result['operations'][0]['operation'])
            invalid=plan({},'用工作站文件 /data/users/u_a/../u_b/a.yaml 替换 APP 内 etc/a.yaml')
            self.assertEqual('NEEDS_INPUT',invalid['status']);model.assert_not_called()
    def test_only_whole_explicit_requests_take_execution_shortcut(self):
        self.assertEqual({'action':'replay'},quick('执行回灌'))
        for value in ('执行回灌吗？','不要执行回灌','“执行回灌”','执行回灌并修改SO','执行回灌，YAML改成true'):
            self.assertIsNone(quick(value))
        value=quick('评测任务 gw-20261024-7bc45b2a020e4e')
        self.assertEqual('evaluate',value['action'])

    def test_model_is_bounded_and_tools_remain_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'runtime').mkdir();skill=root/'.agents/skills/bench-gateway/SKILL.md'
            skill.parent.mkdir(parents=True);skill.write_text('只提取意图。',encoding='utf-8')
            with patch('bench_gateway.parser.subprocess.run',side_effect=subprocess.TimeoutExpired('codex',40)) as model:
                with self.assertRaises(GatewayError) as error:parse({'project_root':str(root)},'请解释这一版材料是否完整',{},[])
            self.assertIn('尚未提交',str(error.exception));self.assertEqual(40,model.call_args.kwargs['timeout'])
            argv=model.call_args.args[0];self.assertIn('model_reasoning_effort="low"',argv)
            self.assertIn('read-only',argv);self.assertIn('code_mode_host',argv)
