from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EVALUATION_SCRIPT = ROOT / "run_v151_evaluations.ps1"
TRUTH_SCRIPT = ROOT / "stream_v151_truth_to_lpai.ps1"
SCENES = (
    "2026-08-20_10-49-30_HLX34B161T1300638",
    "2026-08-20_10-50-15_HLX34B161T1300638",
    "2026-08-20_10-54-53_HLX34B161T1300638",
    "2026-08-20_10-55-49_HLX34B161T1300638",
    "2026-08-20_11-00-13_HLX34B161T1300638",
    "2026-08-20_11-01-04_HLX34B161T1300638",
    "2026-08-20_11-10-39_HLX34B161T1300638",
    "2026-08-20_11-12-21_HLX34B161T1300638",
    "2026-08-20_11-13-08_HLX34B161T1300638",
    "2026-08-20_11-19-47_HLX34B161T1300638",
    "2026-08-20_11-20-28_HLX34B161T1300638",
    "2026-08-20_11-22-18_HLX34B161T1300638",
    "2026-08-20_11-39-30_HLX34B161T1300638",
    "2026-08-20_11-40-14_HLX34B161T1300638",
    "2026-08-20_11-41-56_HLX34B161T1300638",
    "2026-08-20_11-42-39_HLX34B161T1300638",
    "2026-08-20_11-43-56_HLX34B161T1300638",
    "2026-08-20_11-44-43_HLX34B161T1300638",
)


def powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def write_mock(root: Path) -> Path:
    script = root / "mock-ssh.py"
    states = {}
    for index, scene in enumerate(SCENES):
        if index < 7:
            states[scene] = "EVALUATION_COMPLETE_LEGACY"
        elif index < 9:
            states[scene] = "READY"
        elif index < 12:
            states[scene] = "BLOCKED_PARTIAL"
        else:
            states[scene] = "TRUTH_NOT_COMPLETE"
    source = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json
        import os
        import sys

        STATES = {json.dumps(states, sort_keys=True)}
        args = sys.argv[1:]
        command = args[-1] if args else ""
        scene = next((item for item in STATES if item in command), None)
        if scene is None:
            print("unknown mock command", file=sys.stderr)
            raise SystemExit(2)
        state = STATES[scene]
        base = f"v151-formal-20260918-002-eval-{{scene}}"
        if "bash -s --" in command:
            if os.environ.get("V151_MOCK_PREDICTION_MISSING") == scene:
                state = "PREDICTION_NOT_READY"
            if state == "EVALUATION_COMPLETE_LEGACY":
                print(f"EVALUATION_COMPLETE_LEGACY\\t{{base}}\\t{{base}}-a001")
            elif state == "READY":
                print(f"READY\\t{{base}}\\t{{base}}-a001")
            else:
                print(f"{{state}}\\t{{base}}\\t")
            raise SystemExit(0)
        marker = os.environ.get("V151_MOCK_MARKER")
        if marker:
            with open(marker, "a", encoding="utf-8") as stream:
                stream.write(scene + "\\n")
        if scene == {SCENES[7]!r} and "bash -s --" not in command:
            print("mock evaluation failure", file=sys.stderr)
            raise SystemExit(9)
        if "test -f" in command:
            raise SystemExit(0)
        print("mock evaluation complete")
        """
    )
    script.write_text(source, encoding="utf-8")
    wrapper = root / "mock-ssh.cmd"
    wrapper.write_text(
        f'@"{os.fspath(Path(os.sys.executable))}" "{os.fspath(script)}" %*\r\n',
        encoding="utf-8",
    )
    return wrapper


@unittest.skipUnless(powershell(), "PowerShell is required for v151 script tests")
class V151PowerShellTests(unittest.TestCase):
    def run_script(
        self,
        script: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [powershell(), "-NoProfile", "-File", str(script), *arguments],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            env=env,
        )

    def test_scripts_parse(self) -> None:
        for script in (EVALUATION_SCRIPT, TRUTH_SCRIPT):
            command = (
                "$errors=$null; [void][System.Management.Automation.Language.Parser]::"
                f"ParseFile('{script}', [ref]$null, [ref]$errors); "
                "if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
            )
            completed = subprocess.run(
                [powershell(), "-NoProfile", "-Command", command],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_plan_only_reports_seven_complete_two_ready_nine_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mock = write_mock(Path(temp))
            completed = self.run_script(
                EVALUATION_SCRIPT,
                "-PlanOnly",
                "-AsJson",
                "-SshExecutable",
                str(mock),
                "-SshConfigPath",
                str(Path(temp) / "config"),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            value = json.loads(completed.stdout)
            counts = {item["State"]: item["Count"] for item in value["Summary"]}
            self.assertEqual(7, counts["EVALUATION_COMPLETE_LEGACY"])
            self.assertEqual(2, counts["READY"])
            self.assertEqual(3, counts["BLOCKED_PARTIAL"])
            self.assertEqual(6, counts["TRUTH_NOT_COMPLETE"])
            self.assertEqual(9, counts["BLOCKED_PARTIAL"] + counts["TRUTH_NOT_COMPLETE"])
            self.assertEqual(18, value["Categories"]["Requested"])
            self.assertEqual(7, value["Categories"]["ReusedLegacy"])
            self.assertEqual(2, value["Categories"]["Ready"])
            self.assertEqual(9, value["Categories"]["Blocked"])
            self.assertEqual(0, value["Categories"]["Failed"])
            self.assertEqual(2, len(value["Ready"]))

    def test_ready_only_respects_scene_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mock = write_mock(Path(temp))
            selected = f"{SCENES[6]},{SCENES[7]},{SCENES[9]}"
            completed = self.run_script(
                EVALUATION_SCRIPT,
                "-ReadyOnly",
                "-AsJson",
                "-Scene",
                selected,
                "-SshExecutable",
                str(mock),
                "-SshConfigPath",
                str(Path(temp) / "config"),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            value = json.loads(completed.stdout)
            self.assertEqual({SCENES[6], SCENES[7], SCENES[9]}, {item["Scene"] for item in value["Plan"]})
            self.assertEqual({SCENES[7]}, {item["Scene"] for item in value["Ready"]})

    def test_scene_level_prediction_readiness_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mock = write_mock(Path(temp))
            environment = dict(os.environ)
            environment["V151_MOCK_PREDICTION_MISSING"] = SCENES[7]
            completed = self.run_script(
                EVALUATION_SCRIPT,
                "-PlanOnly",
                "-AsJson",
                "-Scene",
                SCENES[7],
                "-SshExecutable",
                str(mock),
                "-SshConfigPath",
                str(Path(temp) / "config"),
                env=environment,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            value = json.loads(completed.stdout)
            self.assertEqual("PREDICTION_NOT_READY", value["Plan"][0]["State"])
            self.assertEqual(0, value["Categories"]["Ready"])
            self.assertEqual(1, value["Categories"]["Blocked"])

    def test_truth_plan_only_accepts_comma_separated_scene_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mock = write_mock(Path(temp))
            completed = self.run_script(
                TRUTH_SCRIPT,
                "-PlanOnly",
                "-AsJson",
                "-Scene",
                f"{SCENES[6]},{SCENES[9]}",
                "-SshExecutable",
                str(mock),
                "-SshConfigPath",
                str(Path(temp) / "config"),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            value = json.loads(completed.stdout)
            self.assertEqual(
                {SCENES[6], SCENES[9]},
                {item["Scene"] for item in value["Results"]},
            )

    def test_one_evaluation_failure_does_not_prevent_other_ready_scene(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mock = write_mock(Path(temp))
            marker = Path(temp) / "executed.txt"
            environment = dict(os.environ)
            environment["V151_MOCK_MARKER"] = str(marker)
            completed = self.run_script(
                EVALUATION_SCRIPT,
                "-Scene",
                f"{SCENES[7]},{SCENES[8]}",
                "-ThrottleLimit",
                "2",
                "-SshExecutable",
                str(mock),
                "-SshConfigPath",
                str(Path(temp) / "config"),
                env=environment,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("one or more v151 evaluation attempts", completed.stderr)
            executed = marker.read_text(encoding="utf-8").splitlines()
            self.assertIn(SCENES[7], executed)
            self.assertIn(SCENES[8], executed)

    def test_unknown_scene_fails_before_ssh(self) -> None:
        completed = self.run_script(EVALUATION_SCRIPT, "-PlanOnly", "-Scene", "unknown-scene")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("unknown v151 scene", completed.stderr)


if __name__ == "__main__":
    unittest.main()
