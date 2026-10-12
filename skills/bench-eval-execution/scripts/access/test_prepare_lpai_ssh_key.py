from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("prepare_lpai_ssh_key.ps1")


def write_agent(path: Path, public_key: Path) -> Path:
    path.write_text(
        "@echo off\r\n"
        f'if "%1"=="-l" (ssh-keygen -lf "{public_key}" -E sha256 & exit /b 0)\r\n'
        "exit /b 0\r\n",
        encoding="ascii",
    )
    return path


def write_denied_agent(path: Path) -> Path:
    path.write_text(
        "@echo off\r\n"
        "echo Error connecting to agent: Permission denied 1>&2\r\n"
        "exit /b 2\r\n",
        encoding="ascii",
    )
    return path


class PrepareLpaiKeyTests(unittest.TestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_missing_key_requires_explicit_authorization_without_running_keygen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            never = root / "never.cmd"
            never.write_text("@echo off\r\nexit /b 99\r\n", encoding="ascii")
            result = self.run_script(
                "-SshDirectory", str(root / "ssh"),
                "-SshKeygenExecutable", str(never),
                "-SshAddExecutable", str(never),
            )
        self.assertEqual(3, result.returncode)
        value = json.loads(result.stdout)
        self.assertEqual("LPAI_KEY_MISSING", value["issues"][0]["code"])
        self.assertEqual("create_personal_lpai_key", value["facts"]["required_authorization"])

    def test_authorized_creation_outputs_only_public_registration_material(self) -> None:
        real_keygen = shutil.which("ssh-keygen")
        self.assertIsNotNone(real_keygen)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "ssh" / "lpai_test_ed25519"
            agent = write_agent(root / "ssh-add.cmd", Path(f"{identity}.pub"))
            result = self.run_script(
                "-SshDirectory", str(identity.parent),
                "-IdentityPath", str(identity),
                "-CredentialName", "lpai-engineer-pc",
                "-AuthorizeCreate", "-NoPassphrase",
                "-SshKeygenExecutable", str(real_keygen),
                "-SshAddExecutable", str(agent),
            )
            private_text = identity.read_text(encoding="ascii")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual("READY_TO_REGISTER", value["status"])
        self.assertTrue(value["safe_to_register"])
        self.assertFalse(value["facts"]["identity_preexisting"])
        self.assertTrue(value["facts"]["public_key"].startswith("ssh-ed25519 "))
        self.assertTrue(value["facts"]["identity_fingerprint"].startswith("SHA256:"))
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", result.stdout)
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", private_text)

    def test_existing_key_is_idempotent(self) -> None:
        real_keygen = shutil.which("ssh-keygen")
        self.assertIsNotNone(real_keygen)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "lpai_existing"
            created = subprocess.run(
                [real_keygen, "-t", "ed25519", "-f", str(identity), "-N", ""],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, created.returncode, created.stderr)
            agent = write_agent(root / "ssh-add.cmd", Path(f"{identity}.pub"))
            result = self.run_script(
                "-IdentityPath", str(identity),
                "-CredentialName", "lpai-existing-pc",
                "-SshKeygenExecutable", str(real_keygen),
                "-SshAddExecutable", str(agent),
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value["mutations_performed"])
        self.assertFalse(value["facts"]["local_changes_this_run"])
        self.assertTrue(value["facts"]["identity_preexisting"])
        self.assertIn("existing LPAI alias", value["facts"]["next_action"])

    def test_agent_permission_denied_does_not_generate_another_key(self) -> None:
        real_keygen = shutil.which("ssh-keygen")
        self.assertIsNotNone(real_keygen)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "lpai_existing"
            created = subprocess.run(
                [real_keygen, "-t", "ed25519", "-f", str(identity), "-N", ""],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, created.returncode, created.stderr)
            denied_agent = write_denied_agent(root / "ssh-add-denied.cmd")
            result = self.run_script(
                "-IdentityPath", str(identity),
                "-CredentialName", "lpai-existing-pc",
                "-SshKeygenExecutable", str(real_keygen),
                "-SshAddExecutable", str(denied_agent),
            )
        self.assertEqual(3, result.returncode)
        value = json.loads(result.stdout)
        self.assertEqual("SSH_AGENT_ACCESS_DENIED", value["issues"][0]["code"])
        self.assertTrue(value["facts"]["identity_preexisting"])


if __name__ == "__main__":
    unittest.main()
