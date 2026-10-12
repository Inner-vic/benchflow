from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from diagnose_workstation_ssh import classify_ssh_failure, inspect, runner_capabilities_command


class WorkstationDiagnosisTests(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        identity = root / "id_ed25519"
        identity.write_text("test-only", encoding="utf-8")
        config = root / "config"
        config.write_text(
            "\n".join((
                "Host ubuntu-workstation",
                "    HostName workstation.example",
                "    User shared",
                f"    IdentityFile {identity.as_posix()}",
            )) + "\n",
            encoding="utf-8",
        )
        return temp, config, identity

    def inspect_with_capabilities(self, capabilities: dict[str, object], *, expected_protocol: str, runner_entry: str = "~/bench-eval/bin/bench-eval") -> dict[str, object]:
        temp, config, identity = self.fixture()
        with temp:
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname workstation.example\nuser shared\nport 22\nidentityfile {identity.as_posix()}\n", stderr="")
            remote = SimpleNamespace(returncode=0, stdout=json.dumps(capabilities), stderr="")
            with patch("diagnose_workstation_ssh.shutil.which", return_value="ssh"), patch("diagnose_workstation_ssh.subprocess.run", side_effect=(expanded, remote)):
                return inspect("ubuntu-workstation", config, expected_protocol, 5, False, runner_entry)

    def test_legacy_job_is_accepted_by_current_runner(self) -> None:
        result = self.inspect_with_capabilities({"runner_protocol": "1.1", "accepted_runner_protocols": ["1.0", "1.1"]}, expected_protocol="1.0")
        self.assertEqual("READY", result["status"])
        self.assertTrue(result["safe_to_submit"])
        self.assertEqual("bench-workstation-access/v1", result["contract"])

    def test_target_job_is_accepted_when_listed(self) -> None:
        result = self.inspect_with_capabilities({"runner_protocol": "1.1", "accepted_runner_protocols": ["1.0", "1.1"]}, expected_protocol="1.1")
        self.assertEqual("READY", result["status"])

    def test_unlisted_job_protocol_is_blocked(self) -> None:
        result = self.inspect_with_capabilities({"runner_protocol": "1.1", "accepted_runner_protocols": ["1.1"]}, expected_protocol="1.0")
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("RUNNER_PROTOCOL_MISMATCH", result["issues"][0]["code"])
        self.assertEqual("READY", result["checks"]["pc_to_workstation"])
        self.assertEqual("BLOCKED", result["checks"]["runner_handshake"])

    def test_missing_accepted_protocols_is_blocked(self) -> None:
        result = self.inspect_with_capabilities({"runner_protocol": "1.1"}, expected_protocol="1.1")
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("RUNNER_PROTOCOL_MISMATCH", result["issues"][0]["code"])

    def test_no_activate_python_entry_is_supported(self) -> None:
        command = runner_capabilities_command("/home/shared/bench-eval/releases/r1/automation/bench_eval.py")
        self.assertEqual("python3 /home/shared/bench-eval/releases/r1/automation/bench_eval.py capabilities", command)

    def test_shell_syntax_in_runner_entry_is_rejected(self) -> None:
        result = self.inspect_with_capabilities({"accepted_runner_protocols": ["1.1"]}, expected_protocol="1.1", runner_entry="~/bench-eval/current/x;touch")
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("RUNNER_ENTRY_INVALID", result["issues"][0]["code"])

    def test_config_only_never_authorizes_submission(self) -> None:
        temp, config, identity = self.fixture()
        with temp:
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname workstation.example\nuser shared\nport 22\nidentityfile {identity.as_posix()}\n", stderr="")
            with patch("diagnose_workstation_ssh.shutil.which", return_value="ssh"), patch("diagnose_workstation_ssh.subprocess.run", return_value=expanded):
                result = inspect("ubuntu-workstation", config, "1.1", 5, True)
        self.assertEqual("LOCAL_CONFIG_READY", result["status"])
        self.assertFalse(result["safe_to_submit"])

    def test_expired_account_password_is_not_classified_as_key_failure(self) -> None:
        result = classify_ssh_failure("WARNING: Your password has expired.\nPassword change required but no TTY available.")
        self.assertEqual("WORKSTATION_ACCOUNT_PASSWORD_EXPIRED", result["code"])


if __name__ == "__main__":
    unittest.main()
