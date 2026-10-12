from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from diagnose_agent_forwarding import inspect


class AgentForwardingTests(unittest.TestCase):
    def config(self, root: Path) -> Path:
        path = root / "config"
        path.write_text("Host ubuntu-workstation\n    HostName workstation.example\n", encoding="utf-8")
        return path

    def lpai_config(self, root: Path, identity: Path) -> Path:
        (root / "known_hosts").write_text("[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", encoding="ascii")
        path = root / "config"
        path.write_text(
            "Host ubuntu-workstation\n    HostName workstation.example\n"
            "Host lpai-engineer\n    HostName ssh-d.lpai.example\n    User root\n    Port 30438\n"
            f"    IdentityFile {identity.as_posix()}\n",
            encoding="utf-8",
        )
        return path

    def test_missing_agent_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            no_agent = SimpleNamespace(returncode=2, stdout="", stderr="Could not open a connection to your authentication agent")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add")), patch("diagnose_agent_forwarding.subprocess.run", return_value=no_agent):
                result = inspect("ubuntu-workstation", config, 5)
        self.assertEqual("SSH_AGENT_NOT_RUNNING", result["issues"][0]["code"])

    def test_agent_permission_denied_is_not_treated_as_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            denied = SimpleNamespace(returncode=2, stdout="", stderr="Error connecting to agent: Permission denied")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add")), patch("diagnose_agent_forwarding.subprocess.run", return_value=denied):
                result = inspect("ubuntu-workstation", config, 5)
        self.assertEqual("SSH_AGENT_ACCESS_DENIED", result["issues"][0]["code"])

    def test_forwarded_socket_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            identities = SimpleNamespace(returncode=0, stdout="key", stderr="")
            no_socket = SimpleNamespace(returncode=1, stdout="", stderr="")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(identities, no_socket)):
                result = inspect("ubuntu-workstation", config, 5)
        self.assertEqual("AGENT_FORWARDING_NOT_ACTIVE", result["issues"][0]["code"])

    def test_ready_without_lpai_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            ok = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(ok, ok)):
                result = inspect("ubuntu-workstation", config, 5)
        self.assertEqual("READY", result["status"])
        self.assertEqual("NOT_REQUESTED", result["checks"]["lpai_read_only_handshake"])

    def test_lpai_auth_failure_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            ok = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            denied = SimpleNamespace(returncode=255, stdout="", stderr="Permission denied (publickey)")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(ok, ok, denied)):
                result = inspect("ubuntu-workstation", config, 5, "ssh-d.lpai.example", "root", 30438)
        self.assertEqual("LPAI_AUTH_FAILED", result["issues"][0]["code"])

    def test_alias_requires_its_exact_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "engineer-lpai-key"
            config = self.lpai_config(root, missing)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {missing.as_posix()}\n", stderr="")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", return_value=expanded):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer")
        self.assertEqual("LPAI_PRIVATE_KEY_MISSING", result["issues"][0]["code"])
        self.assertIn("无密码兜底", result["issues"][0]["action"])

    def test_alias_requires_exact_key_loaded_in_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "engineer-lpai-key"
            identity.write_text("test-only", encoding="ascii")
            Path(f"{identity}.pub").write_text("ssh-ed25519 AAAAENGINEER engineer\n", encoding="ascii")
            config = self.lpai_config(root, identity)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {identity.as_posix()}\nuserknownhostsfile {(root / 'known_hosts').as_posix()}\n", stderr="")
            fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            host_record = SimpleNamespace(returncode=0, stdout="[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", stderr="")
            other_key = SimpleNamespace(returncode=0, stdout="256 SHA256:OTHER other (ED25519)\n", stderr="")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(expanded, fingerprint, host_record, other_key)):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer")
        self.assertEqual("LPAI_KEY_NOT_LOADED", result["issues"][0]["code"])

    def test_alias_verifies_same_fingerprint_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "engineer-lpai-key"
            identity.write_text("test-only", encoding="ascii")
            Path(f"{identity}.pub").write_text("ssh-ed25519 AAAAENGINEER engineer\n", encoding="ascii")
            config = self.lpai_config(root, identity)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {identity.as_posix()}\nuserknownhostsfile {(root / 'known_hosts').as_posix()}\n", stderr="")
            fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            host_record = SimpleNamespace(returncode=0, stdout="[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", stderr="")
            exact_key = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            forwarded = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            ok = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(expanded, fingerprint, host_record, exact_key, forwarded, ok)):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer")
        self.assertEqual("READY", result["status"])
        self.assertEqual("SHA256:ENGINEER", result["facts"]["lpai_identity_fingerprint"])
        self.assertEqual("EXACT_FORWARDED_KEY_VIA_PUBLIC_KEY", result["facts"]["lpai_identity_selection"])
        self.assertEqual("PINNED_FROM_LOCAL_TRUST_STORE", result["facts"]["lpai_host_key_policy"])

    def test_alias_rejects_forwarding_a_different_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "engineer-lpai-key"
            identity.write_text("test-only", encoding="ascii")
            Path(f"{identity}.pub").write_text("ssh-ed25519 AAAAENGINEER engineer\n", encoding="ascii")
            config = self.lpai_config(root, identity)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {identity.as_posix()}\nuserknownhostsfile {(root / 'known_hosts').as_posix()}\n", stderr="")
            fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            host_record = SimpleNamespace(returncode=0, stdout="[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", stderr="")
            exact_key = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            forwarded_other = SimpleNamespace(returncode=0, stdout="256 SHA256:OTHER other (ED25519)\n", stderr="")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(expanded, fingerprint, host_record, exact_key, forwarded_other)):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer")
        self.assertEqual("LPAI_KEY_NOT_FORWARDED", result["issues"][0]["code"])

    def test_remote_connection_refused_points_to_instance_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "engineer-lpai-key"
            identity.write_text("test-only", encoding="ascii")
            Path(f"{identity}.pub").write_text("ssh-ed25519 AAAAENGINEER engineer\n", encoding="ascii")
            config = self.lpai_config(root, identity)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {identity.as_posix()}\nuserknownhostsfile {(root / 'known_hosts').as_posix()}\n", stderr="")
            fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            host_record = SimpleNamespace(returncode=0, stdout="[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", stderr="")
            exact_key = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            forwarded = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            refused = SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect to host ssh-d.lpai.example port 30438: Connection refused")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(expanded, fingerprint, host_record, exact_key, forwarded, refused)):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer")
        self.assertEqual("LPAI_INSTANCE_STOPPED_OR_PORT_CLOSED", result["issues"][0]["code"])
        self.assertIn("开发空间是否已启动", result["issues"][0]["action"])

    def test_remote_timeout_prompts_instance_state_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "engineer-lpai-key"
            identity.write_text("test-only", encoding="ascii")
            Path(f"{identity}.pub").write_text("ssh-ed25519 AAAAENGINEER engineer\n", encoding="ascii")
            config = self.lpai_config(root, identity)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {identity.as_posix()}\nuserknownhostsfile {(root / 'known_hosts').as_posix()}\n", stderr="")
            fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            host_record = SimpleNamespace(returncode=0, stdout="[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", stderr="")
            exact_key = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            forwarded = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            timeout = SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect to host ssh-d.lpai.example port 30438: Connection timed out")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(expanded, fingerprint, host_record, exact_key, forwarded, timeout)):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer")
        self.assertEqual("LPAI_INSTANCE_UNREACHABLE", result["issues"][0]["code"])
        self.assertIn("先到 LPAI 页面确认开发空间已启动", result["issues"][0]["action"])
        self.assertIn("工作站路由/DNS", result["issues"][0]["action"])

    def test_optional_local_probe_classifies_network_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "engineer-lpai-key"
            identity.write_text("test-only", encoding="ascii")
            Path(f"{identity}.pub").write_text("ssh-ed25519 AAAAENGINEER engineer\n", encoding="ascii")
            config = self.lpai_config(root, identity)
            expanded = SimpleNamespace(returncode=0, stdout=f"hostname ssh-d.lpai.example\nuser root\nport 30438\nidentityfile {identity.as_posix()}\nuserknownhostsfile {(root / 'known_hosts').as_posix()}\n", stderr="")
            fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            host_record = SimpleNamespace(returncode=0, stdout="[ssh-d.lpai.example]:30438 ssh-ed25519 AAAAHOST\n", stderr="")
            exact_key = SimpleNamespace(returncode=0, stdout="256 SHA256:ENGINEER engineer (ED25519)\n", stderr="")
            timeout = SimpleNamespace(returncode=255, stdout="", stderr="connect to host ssh-d.lpai.example port 30438: Connection timed out")
            with patch("diagnose_agent_forwarding.shutil.which", side_effect=("ssh", "ssh-add", "ssh-keygen")), patch("diagnose_agent_forwarding.subprocess.run", side_effect=(expanded, fingerprint, host_record, exact_key, timeout)):
                result = inspect("ubuntu-workstation", config, 5, lpai_alias="lpai-engineer", require_local_lpai_probe=True)
        self.assertEqual("LPAI_LOCAL_UNREACHABLE", result["issues"][0]["code"])
        self.assertIn("先到 LPAI 页面确认开发空间已启动", result["issues"][0]["action"])


if __name__ == "__main__":
    unittest.main()
