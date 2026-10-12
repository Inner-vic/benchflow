from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("connect_workstation_ssh.ps1")


def write_cmd(path: Path, content: str) -> Path:
    path.write_text("@echo off\r\n" + content.replace("\n", "\r\n"), encoding="ascii")
    return path


class FixedWorkstationConnectTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        *,
        auth_fails: bool = False,
        accepted_protocols: tuple[str, ...] = ("1.0", "1.1"),
        connection_error: str | None = None,
    ) -> tuple[Path, Path, Path, Path, Path, Path]:
        ssh_dir = root / "ssh"
        ssh_dir.mkdir()
        identity = ssh_dir / "id_ed25519"
        identity.write_text("test-private-placeholder", encoding="ascii")
        identity.with_suffix(".pub").write_text("ssh-ed25519 AAAATEST test\n", encoding="ascii")
        config = ssh_dir / "config"
        config.write_text(
            "Host ubuntu-workstation\n"
            "    HostName 192.0.2.10\n"
            "    User bench-user\n"
            f"    IdentityFile {identity.as_posix()}\n",
            encoding="utf-8",
        )
        capabilities = json.dumps(
            {"runner_version": "1.8.0", "runner_protocol": "1.1", "accepted_runner_protocols": list(accepted_protocols)},
            separators=(",", ":"),
        )
        if auth_fails:
            fail_line = "echo Permission denied (publickey). 1>&2\nexit /b 255\n"
        elif connection_error:
            fail_line = f"echo {connection_error} 1>&2\nexit /b 255\n"
        else:
            fail_line = f"echo {capabilities}\nexit /b 0\n"
        ssh = write_cmd(
            root / "ssh.cmd",
            f'echo %* | findstr /C:"-G" >nul\n'
            "if not errorlevel 1 (\n"
            "  echo hostname 192.0.2.10\n"
            "  echo user bench-user\n"
            "  echo port 22\n"
            f"  echo identityfile {identity.as_posix()}\n"
            "  exit /b 0\n"
            ")\n"
            + fail_line,
        )
        keygen = write_cmd(root / "ssh-keygen.cmd", "exit /b 0\n")
        keyscan = write_cmd(root / "ssh-keyscan.cmd", "exit /b 0\n")
        return ssh_dir, config, identity, ssh, keygen, keyscan

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_existing_setup_uses_one_fast_ssh_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, _, ssh, keygen, keyscan = self.fixture(root)
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("READY", result["status"])
        self.assertEqual(1, result["facts"]["ssh_attempts"])
        self.assertFalse(result["facts"]["local_changes_this_run"])
        self.assertLess(result["facts"]["elapsed_ms"], 2_000)

    def test_missing_config_stops_for_one_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh = write_cmd(root / "ssh.cmd", "exit /b 99\n")
            keygen = write_cmd(root / "ssh-keygen.cmd", "exit /b 99\n")
            keyscan = write_cmd(root / "ssh-keyscan.cmd", "exit /b 99\n")
            completed = self.run_script(
                "-SshDirectory", str(root / "new-ssh"),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(3, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("NEEDS_USER_ACTION", result["status"])
        self.assertEqual("create_personal_key_and_managed_ssh_config", result["facts"]["required_authorization"])
        self.assertEqual(0, result["facts"]["ssh_attempts"])

    def test_auth_failure_returns_the_active_public_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, identity, ssh, keygen, keyscan = self.fixture(root, auth_fails=True)
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(3, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("SSH_AUTH_FAILED", result["issues"][0]["code"])
        self.assertEqual("id_ed25519.pub", Path(result["facts"]["public_key_to_register"]).name)
        self.assertEqual(1, result["facts"]["ssh_attempts"])

    def test_first_use_registers_public_key_by_password_then_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, identity, _, keygen, keyscan = self.fixture(root)
            marker = root / "registered.txt"
            capabilities = '{"runner_version":"1.8.0","runner_protocol":"1.1","accepted_runner_protocols":["1.0","1.1"]}'
            ssh = write_cmd(
                root / "first-use-ssh.cmd",
                f'echo %* | findstr /C:"-G" >nul\n'
                "if not errorlevel 1 (\n"
                "  echo hostname 192.0.2.10\n"
                "  echo user bench-user\n"
                "  echo port 22\n"
                f"  echo identityfile {identity.as_posix()}\n"
                "  exit /b 0\n"
                ")\n"
                'echo %* | findstr /C:"PreferredAuthentications=password,keyboard-interactive" >nul\n'
                'if not errorlevel 1 (\n'
                '  echo %* | findstr /C:"AAAATEST" >nul\n'
                '  if errorlevel 1 exit /b 91\n'
                f'  echo registered>"{marker}"\n'
                '  exit /b 0\n'
                ')\n'
                f'if exist "{marker}" (echo {capabilities} & exit /b 0)\n'
                "echo Permission denied (publickey). 1>&2\n"
                "exit /b 255\n",
            )
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-FirstUse",
                "-NoPassphrase",
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertTrue(completed.stdout.strip(), f"stdout={completed.stdout!r} stderr={completed.stderr!r}")
        result = json.loads(completed.stdout)
        self.assertEqual("READY", result["status"])
        self.assertTrue(result["facts"]["first_use"])
        self.assertEqual("REMOTE_PASSWORD_LOGIN_SUCCEEDED", result["facts"]["public_key_registration"])
        self.assertEqual(2, result["facts"]["ssh_attempts"])
        self.assertEqual("PASSWORD_BOOTSTRAP_THEN_PUBLICKEY_VERIFY", result["facts"]["onboarding_strategy"])

    def test_authorized_first_setup_reaches_ready_in_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir = root / "new-ssh"
            ssh_dir.mkdir()
            unrelated = "Host unrelated\n    HostName unrelated.example\n"
            (ssh_dir / "config").write_text(unrelated, encoding="utf-8")
            identity = ssh_dir / "bench_eval_ed25519"
            real_keygen = shutil.which("ssh-keygen")
            self.assertIsNotNone(real_keygen)
            host_key = root / "host_ed25519"
            generated = subprocess.run(
                [real_keygen, "-t", "ed25519", "-f", str(host_key), "-N", ""],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, generated.returncode, generated.stderr)
            host_key_body = host_key.with_suffix(".pub").read_text(encoding="ascii").split()[1]
            fingerprint_result = subprocess.run(
                [real_keygen, "-lf", str(host_key.with_suffix(".pub")), "-E", "sha256"],
                text=True,
                capture_output=True,
                check=False,
            )
            expected_fingerprint = fingerprint_result.stdout.split()[1]
            (ssh_dir / "known_hosts").write_text(
                f"192.0.2.10 ssh-ed25519 {host_key_body}\n", encoding="ascii"
            )
            ssh = write_cmd(
                root / "ssh.cmd",
                f'echo %* | findstr /C:"-G" >nul\n'
                "if not errorlevel 1 (\n"
                "  echo hostname 192.0.2.10\n"
                "  echo user bench-user\n"
                "  echo port 22\n"
                f"  echo identityfile {identity.as_posix()}\n"
                "  exit /b 0\n"
                ")\n"
                'echo {"runner_version":"1.8.0","runner_protocol":"1.1","accepted_runner_protocols":["1.0","1.1"]}\n'
                "exit /b 0\n",
            )
            keyscan = write_cmd(root / "ssh-keyscan.cmd", f"echo # 192.0.2.10:22 SSH-2.0-test\necho 192.0.2.10 ssh-ed25519 {host_key_body}\nexit /b 0\n")
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-IdentityPath", str(identity),
                "-ExpectedHostFingerprint", expected_fingerprint,
                "-AuthorizeLocalSetup",
                "-NoPassphrase",
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(real_keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
            managed_config = (ssh_dir / "config").read_text(encoding="utf-8")
            second = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-IdentityPath", str(identity),
                "-ExpectedHostFingerprint", expected_fingerprint,
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(real_keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("READY", result["status"])
        self.assertTrue(result["facts"]["local_changes_this_run"])
        self.assertEqual(1, result["facts"]["ssh_attempts"])
        self.assertIn(unrelated.strip(), managed_config)
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        second_result = json.loads(second.stdout)
        self.assertEqual("READY", second_result["status"])
        self.assertFalse(second_result["facts"]["local_changes_this_run"])
        self.assertEqual(1, second_result["facts"]["ssh_attempts"])

    def test_protected_unloaded_key_stops_before_network_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, _, ssh, _, keyscan = self.fixture(root)
            keygen = write_cmd(
                root / "protected-keygen.cmd",
                "if \"%1\"==\"-y\" exit /b 255\n"
                "if \"%1\"==\"-lf\" echo 256 SHA256:TEST test (ED25519)\n"
                "exit /b 0\n",
            )
            ssh_add = write_cmd(root / "ssh-add.cmd", "echo Error connecting to agent 1>&2\nexit /b 2\n")
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
                "-SshAddExecutable", str(ssh_add),
            )
        self.assertEqual(3, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("SSH_KEY_NOT_LOADED", result["issues"][0]["code"])
        self.assertEqual(0, result["facts"]["ssh_attempts"])

    def test_runner_protocol_mismatch_is_not_treated_as_ssh_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, _, ssh, keygen, keyscan = self.fixture(root, accepted_protocols=("1.0",))
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(2, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("RUNNER_PROTOCOL_MISMATCH", result["issues"][0]["code"])
        self.assertEqual("READY", result["checks"]["pc_to_workstation"])
        self.assertEqual("BLOCKED", result["checks"]["runner_handshake"])

    def test_unreachable_workstation_enters_only_network_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, _, ssh, keygen, keyscan = self.fixture(root, connection_error="Connection timed out")
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(2, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("SSH_UNREACHABLE", result["issues"][0]["code"])
        self.assertEqual(1, result["facts"]["ssh_attempts"])

    def test_expired_account_password_blocks_without_key_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir, _, _, ssh, keygen, keyscan = self.fixture(
                root, connection_error="WARNING: Your password has expired. Password change required but no TTY available."
            )
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(2, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("WORKSTATION_ACCOUNT_PASSWORD_EXPIRED", result["issues"][0]["code"])
        self.assertEqual(1, result["facts"]["ssh_attempts"])
        self.assertNotIn("public_key_to_register", result["facts"])

    def test_first_setup_blocks_a_wrong_pinned_host_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssh_dir = root / "new-ssh"
            identity = ssh_dir / "bench_eval_ed25519"
            real_keygen = shutil.which("ssh-keygen")
            self.assertIsNotNone(real_keygen)
            host_key = root / "unexpected_host"
            generated = subprocess.run(
                [real_keygen, "-t", "ed25519", "-f", str(host_key), "-N", ""],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, generated.returncode, generated.stderr)
            host_key_body = host_key.with_suffix(".pub").read_text(encoding="ascii").split()[1]
            ssh = write_cmd(root / "ssh.cmd", "exit /b 99\n")
            keyscan = write_cmd(root / "ssh-keyscan.cmd", f"echo 192.0.2.10 ssh-ed25519 {host_key_body}\nexit /b 0\n")
            completed = self.run_script(
                "-SshDirectory", str(ssh_dir),
                "-IdentityPath", str(identity),
                "-AuthorizeLocalSetup",
                "-NoPassphrase",
                "-SshExecutable", str(ssh),
                "-SshKeygenExecutable", str(real_keygen),
                "-SshKeyscanExecutable", str(keyscan),
            )
        self.assertEqual(2, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("SSH_HOST_KEY_MISMATCH", result["issues"][0]["code"])
        self.assertEqual(0, result["facts"]["ssh_attempts"])


if __name__ == "__main__":
    unittest.main()
