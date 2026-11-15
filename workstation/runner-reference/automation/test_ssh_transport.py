from __future__ import annotations

import unittest

from ssh_transport import (
    KEYBOARD_INTERACTIVE_EMPTY,
    PUBLICKEY,
    auth_args,
    command,
    environment,
    normalize_auth_mode,
)


class SshTransportTests(unittest.TestCase):
    def test_publickey_is_default_and_noninteractive(self) -> None:
        self.assertEqual(PUBLICKEY, normalize_auth_mode(None))
        self.assertEqual(["-o", "BatchMode=yes"], auth_args(PUBLICKEY))
        self.assertNotIn("SSH_ASKPASS", environment(PUBLICKEY, {}))

    def test_empty_interactive_is_single_prompt_without_password(self) -> None:
        args = auth_args(KEYBOARD_INTERACTIVE_EMPTY)
        self.assertIn("PubkeyAuthentication=no", args)
        self.assertIn("PasswordAuthentication=no", args)
        self.assertIn("NumberOfPasswordPrompts=1", args)
        value = environment(KEYBOARD_INTERACTIVE_EMPTY, {})
        self.assertEqual("/bin/true", value["SSH_ASKPASS"])
        self.assertEqual("force", value["SSH_ASKPASS_REQUIRE"])

    def test_auth_policy_precedes_caller_args(self) -> None:
        argv = command("ssh", ["-F", "/safe/config"], KEYBOARD_INTERACTIVE_EMPTY)
        self.assertEqual("ssh", argv[0])
        self.assertLess(argv.index("BatchMode=no"), argv.index("/safe/config"))

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_auth_mode("password")


if __name__ == "__main__":
    unittest.main()
