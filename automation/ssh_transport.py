#!/usr/bin/env python3
"""Shared, non-secret SSH authentication policy for bench-eval transports."""

from __future__ import annotations

import os
from typing import Mapping, Sequence


PUBLICKEY = "publickey"
KEYBOARD_INTERACTIVE_EMPTY = "keyboard-interactive-empty"
SUPPORTED_AUTH_MODES = (PUBLICKEY, KEYBOARD_INTERACTIVE_EMPTY)


def normalize_auth_mode(value: object) -> str:
    mode = PUBLICKEY if value in (None, "") else str(value)
    if mode not in SUPPORTED_AUTH_MODES:
        raise ValueError(
            f"unsupported ssh_auth_mode {mode!r}; expected one of {SUPPORTED_AUTH_MODES}"
        )
    return mode


def auth_args(mode: object) -> list[str]:
    selected = normalize_auth_mode(mode)
    if selected == PUBLICKEY:
        return ["-o", "BatchMode=yes"]
    return [
        "-o", "BatchMode=no",
        "-o", "PubkeyAuthentication=no",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=yes",
        "-o", "PreferredAuthentications=keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1",
    ]


def command(binary: str, configured_args: Sequence[str], mode: object) -> list[str]:
    if not isinstance(binary, str) or not binary:
        raise ValueError("ssh binary must be a non-empty string")
    if any(not isinstance(item, str) for item in configured_args):
        raise ValueError("ssh arguments must be strings")
    return [binary, *auth_args(mode), *configured_args]


def environment(mode: object, base: Mapping[str, str] | None = None) -> dict[str, str]:
    selected = normalize_auth_mode(mode)
    result = dict(os.environ if base is None else base)
    if selected == KEYBOARD_INTERACTIVE_EMPTY:
        result.update({
            "SSH_ASKPASS": "/bin/true",
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": result.get("DISPLAY") or "bench-eval",
        })
    return result
