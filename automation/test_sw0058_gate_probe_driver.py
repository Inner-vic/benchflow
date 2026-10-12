import json
import subprocess
import unittest
from unittest.mock import patch

import sw0058_gate_probe_driver as driver


class BenchClaimGuardTests(unittest.TestCase):
    def test_rejects_prior_run_without_a_runner_lock(self):
        old_status = {"schema_version": 1, "run_id": "probe-011", "artifacts": []}
        with patch.object(driver.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(old_status), ""
        )):
            with self.assertRaisesRegex(driver.ProbeError, "does not own"):
                driver.require_bench_claim("probe-011")

    def test_accepts_only_the_matching_claimed_run(self):
        claimed = {"runner": {
            "run": {"state": "BENCH_CLAIMED"},
            "owns_bench": True,
            "bench_lock": {"run_id": "probe-012"},
        }}
        with patch.object(driver.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(claimed), ""
        )):
            driver.require_bench_claim("probe-012")
            with self.assertRaisesRegex(driver.ProbeError, "does not own"):
                driver.require_bench_claim("probe-013")


class OfficialAppRestoreTests(unittest.TestCase):
    def test_candidate_config_restores_under_temporary_permissive_mode(self):
        commands = []
        with patch.object(driver, "bench", side_effect=lambda command, **_kwargs: commands.append(command)):
            driver.restore_candidate_config("/backup/factory.yaml", "Enforcing")
        self.assertEqual(len(commands), 1)
        self.assertIn("/usr/sbin/setenforce 0", commands[0])
        self.assertIn("trap '/usr/sbin/setenforce 1' EXIT", commands[0])
        self.assertIn("cp -p /backup/factory.yaml", commands[0])

    def test_restore_uses_service_manager_and_original_security_mode(self):
        initial = {"fusion": "active", "replay": "inactive",
                   "selinux": "Enforcing", "xrecorder": True}
        commands = []

        def record(command, **_kwargs):
            commands.append(command)
            if "is-active infer_expert_pcm_replay" in command and "unloadsdf" not in command:
                return "inactive\n"
            return ""

        with patch.object(driver, "bench", side_effect=record):
            driver.restore_formal_app(initial, mounted=True)

        joined = "\n".join(commands)
        self.assertIn("unloadsdf infer_expert_pcm_replay", joined)
        self.assertIn("umount /app/livc", joined)
        self.assertIn("loadsdf /app/livc/sdf/infer_expert_fusion.sdf", joined)
        self.assertIn("/usr/sbin/setenforce 1", joined)
        self.assertIn("x_recorder", joined)
        self.assertNotIn("/app/livc/bin/bench-replay-mode", joined)
        self.assertLess(joined.index("umount /app/livc"),
                        joined.index("loadsdf /app/livc/sdf/infer_expert_fusion.sdf"))

    def test_restore_refuses_unmount_while_candidate_still_active(self):
        initial = {"fusion": "active", "replay": "inactive",
                   "selinux": "Enforcing", "xrecorder": True}

        def active(command, **_kwargs):
            return "active\n" if command.endswith("is-active infer_expert_pcm_replay") else ""

        with patch.object(driver, "bench", side_effect=active), patch.object(driver.time, "sleep"):
            with self.assertRaisesRegex(driver.ProbeError, "did not stop"):
                driver.restore_formal_app(initial, mounted=True)


class ProbeConfigTests(unittest.TestCase):
    def test_uses_reviewed_bench_mode_fallback(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "source.yaml"
            source.write_text("replay:\n  enable: OFF\nbench_mode:\n  enable: False\n"
                              "speed_gate:\n  enable: true\nperception_gate:\n  enable: true\n"
                              "apa_gate:\n  enable: true\ngear_gate:\n  enable: true\n"
                              "eval_dump:\n  enable: OFF\n  output_dir: /old\n  scene_root: /old\n")
            destination = root / "candidate.yaml"
            job = {"formal_app": {"prediction_root": "/new/pred",
                                  "scene_root": "/new/scene"}}
            receipt = driver.prepare_probe_config(source, destination, job)
            self.assertIn("bench_mode:\n  enable: True", destination.read_text())
            self.assertIn({"key": "bench_mode.enable", "from": "False", "to": "True"},
                          receipt["changes"])
            self.assertIn("bench_mode:\n  enable: False", source.read_text())


if __name__ == "__main__":
    unittest.main()
