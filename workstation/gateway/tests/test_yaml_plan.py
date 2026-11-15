import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError
from bench_gateway.yaml_plan import plan, validate_plan


def set_op(target="config/a.yaml", path=None, value="false"):
    return {"operation": "set", "target": target, "path": path or ["camera", "enabled"],
            "value_json": value, "source": ""}


def ready(*ops):
    return {"status": "READY", "question": "", "operations": list(ops)}


class YamlPlanTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise the model boundary; deterministic literals are covered separately.
        shortcut=patch("bench_gateway.yaml_plan.quick_plan",return_value=None)
        shortcut.start();self.addCleanup(shortcut.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {"project_root": str(self.root), "codex_binary": "codex-test",
                    "yaml_plan_timeout_seconds": 40, "app_secret": "PRIVATE_SECRET_SENTINEL"}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def respond(data, stdout="", code=0):
        def execute(argv, **kwargs):
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(data), encoding="utf-8")
            return SimpleNamespace(returncode=code, stdout=stdout)
        return execute

    def test_natural_language_uses_only_request_and_tool_disabled_command(self):
        request = "把 config/a.yaml 的 camera.enabled 改为 false"
        (self.root / "original.yaml").write_text("password: YAML_SECRET_SENTINEL", encoding="utf-8")
        with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=self.respond(ready(set_op()))) as run:
            result = plan(self.cfg, request)
        self.assertEqual(result["status"], "READY")
        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(argv[0], "codex-test")
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--sandbox", "--output-schema"):
            self.assertIn(flag, argv)
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "hooks"):
            self.assertIn(feature, argv)
        self.assertIn(request, kwargs["input"])
        self.assertNotIn("PRIVATE_SECRET_SENTINEL", kwargs["input"])
        self.assertNotIn("YAML_SECRET_SENTINEL", kwargs["input"])
        self.assertEqual(kwargs["timeout"], 40)
        self.assertEqual(list((self.root / "runtime").iterdir()), [])

    def test_scalar_and_nested_json_types_are_preserved(self):
        for text, expected in (("false", False), ('"false"', "false"), ("12", 12),
                               ("1.25", 1.25), ("null", None), ('{"x":[1,true]}', {"x": [1, True]})):
            result = validate_plan(ready(set_op(value=text)), "config/a.yaml camera.enabled " + text)
            value = json.loads(result["operations"][0]["value_json"])
            self.assertEqual(value, expected)
            self.assertIs(type(value), type(expected))

    def test_explicit_numeric_path_is_accepted_without_inventing_index(self):
        result = validate_plan(ready(set_op(path=["cameras", "0", "enabled"])),
                               "config/a.yaml cameras[0].enabled=false")
        self.assertEqual(result["operations"][0]["path"], ["cameras", "0", "enabled"])
        with self.assertRaises(GatewayError):
            validate_plan(ready(set_op(path=["cameras", "0", "enabled"])),
                          "config/a.yaml 的第一个 cameras.enabled=false")

    def test_replace_requires_both_user_supplied_source_and_target(self):
        op = {"operation": "replace", "target": "config/a.yaml", "path": [], "value_json": "",
              "source": "/mnt/data/users/alice/inbox/new.yaml"}
        result = validate_plan(ready(op), "用 " + op["source"] + " 替换 config/a.yaml")
        self.assertEqual(result["status"], "READY")
        for request in ("替换 config/a.yaml", "用 " + op["source"] + " 替换", "替换两个文件"):
            with self.subTest(request=request), self.assertRaises(GatewayError):
                validate_plan(ready(op), request)
        op["target"] = "new.yaml"
        with self.assertRaises(GatewayError):
            validate_plan(ready(op), "用 " + op["source"] + " 替换")

    def test_invented_paths_and_field_substrings_rejected(self):
        for request in ("config/a.yaml camera.model=false", "other.yaml camera.enabled=false",
                        "config/a.yaml camera.enabledd=false", "config/a.yaml，随便改个值"):
            with self.subTest(request=request), self.assertRaises(GatewayError):
                validate_plan(ready(set_op()), request)

    def test_new_value_cannot_be_guessed_when_only_file_and_field_are_provided(self):
        for value in ("false", "12", '"model-a"', "null"):
            with self.subTest(value=value), self.assertRaises(GatewayError):
                validate_plan(ready(set_op(value=value)), "把 config/a.yaml 的 camera.enabled 改一下")
        self.assertEqual(validate_plan(ready(set_op()), "关闭 config/a.yaml 的 camera.enabled")["status"], "READY")
        self.assertEqual(validate_plan(ready(set_op(value="1e3")), "config/a.yaml camera.enabled 改为 1e3")["status"], "READY")

    def test_target_traversal_absolute_and_template_paths_rejected(self):
        for target in ("../a.yaml", "/app/a.yaml", "config/../a.yaml", "a.yaml/", "a.yaml.exe",
                       "C:/app/a.yaml", "config\\a.yaml", "./a.yaml", "config//a.yaml", "$(x).yaml"):
            with self.subTest(target=target), self.assertRaises(GatewayError):
                validate_plan(ready(set_op(target=target)), target + " camera.enabled=false")

    def test_replace_source_must_be_safe_absolute_path(self):
        for source in ("new.yaml", "../new.yaml", "/x/../new.yaml", "https://example/new.yaml",
                       "C:/new.yaml", "//x/new.yaml", "/x/$(touch a).yaml", "/x/new.yaml\n"):
            op = {"operation": "replace", "target": "a.yaml", "path": [], "value_json": "", "source": source}
            with self.subTest(source=source), self.assertRaises(GatewayError):
                validate_plan(ready(op), "用 " + source + " 替换 a.yaml")

    def test_nonfinite_duplicate_keys_and_expressions_rejected(self):
        for value in ("NaN", "Infinity", "1e9999", '{"a":1,"a":2}', '"$(touch /tmp/x)"',
                      '"`rm -rf x`"', '"${SECRET}"', "__import__('os')", "[" * 1000):
            with self.subTest(value=value), self.assertRaises(GatewayError):
                validate_plan(ready(set_op(value=value)), "config/a.yaml camera.enabled=" + value)

    def test_max_four_files_twenty_operations_and_no_duplicate_target_paths(self):
        operations = [set_op(target=f"a{i}.yaml") for i in range(5)]
        request = " ".join(op["target"] for op in operations) + " camera.enabled=false"
        with self.assertRaises(GatewayError):
            validate_plan(ready(*operations), request)
        operations = [set_op(path=[f"key{i}"]) for i in range(21)]
        request = "config/a.yaml " + " ".join(f"key{i}" for i in range(21))
        with self.assertRaises(GatewayError):
            validate_plan(ready(*operations), request)
        with self.assertRaises(GatewayError):
            validate_plan(ready(set_op(), set_op()), "config/a.yaml camera.enabled=false")

    def test_missing_information_returns_question_without_operations(self):
        data = {"status": "NEEDS_INPUT", "question": "请提供完整字段路径和新值。", "operations": []}
        with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=self.respond(data)):
            self.assertEqual(plan(self.cfg, "修改 a.yaml"), data)
        data["operations"] = [set_op()]
        with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=self.respond(data)):
            self.assertEqual(plan(self.cfg, "修改 a.yaml")["operations"], [])

    def test_timeout_is_capped_and_not_retried(self):
        self.cfg["yaml_plan_timeout_seconds"] = 1000
        with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 90)) as run:
            result = plan(self.cfg, "config/a.yaml camera.enabled=false")
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("超时", result["question"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 90)
        self.cfg["yaml_plan_timeout_seconds"] = 0
        with patch("bench_gateway.yaml_plan.subprocess.run") as run:
            for timeout in (0, -1, float("nan"), float("inf")):
                self.cfg["yaml_plan_timeout_seconds"] = timeout
                self.assertEqual(plan(self.cfg, "a.yaml x=1")["status"], "NEEDS_INPUT")
            run.assert_not_called()

    def test_tool_events_and_process_failure_rejected_even_if_output_exists(self):
        request = "config/a.yaml camera.enabled=false"
        for phase in ("item.started", "item.updated", "item.completed"):
            stdout = json.dumps({"type": phase, "item": {"type": "command_execution"}})
            with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=self.respond(ready(set_op()), stdout)):
                self.assertEqual(plan(self.cfg, request)["status"], "NEEDS_INPUT")
        with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=self.respond(ready(set_op()), code=1)):
            self.assertEqual(plan(self.cfg, request)["status"], "NEEDS_INPUT")

    def test_extra_operations_keys_and_missing_output_fail_closed(self):
        malformed = ready(set_op())
        malformed["command"] = "touch /tmp/x"
        with patch("bench_gateway.yaml_plan.subprocess.run", side_effect=self.respond(malformed)):
            self.assertEqual(plan(self.cfg, "config/a.yaml camera.enabled=false")["status"], "NEEDS_INPUT")
        with patch("bench_gateway.yaml_plan.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="")):
            self.assertEqual(plan(self.cfg, "config/a.yaml camera.enabled=false")["status"], "NEEDS_INPUT")

    def test_empty_and_oversized_requests_do_not_invoke_model(self):
        with patch("bench_gateway.yaml_plan.subprocess.run") as run:
            for request in ("", " " * 5, "x" * 12001, None):
                self.assertEqual(plan(self.cfg, request)["status"], "NEEDS_INPUT")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
