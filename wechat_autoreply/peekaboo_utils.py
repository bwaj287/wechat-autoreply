from __future__ import annotations

import os
import subprocess
from typing import Iterable

from .paths import BRIDGE_SOCKET, PEEKABOO


def run(cmd: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"command failed: {cmd}")
    return proc


def peekaboo_commands(args: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    asuser_prefix = ["launchctl", "asuser", str(os.getuid())]
    binary = str(PEEKABOO)
    if BRIDGE_SOCKET.exists():
        commands.append([*asuser_prefix, binary, args[0], "--bridge-socket", str(BRIDGE_SOCKET), *args[1:]])
        commands.append([*asuser_prefix, binary, args[0], "--no-remote", "--bridge-socket", str(BRIDGE_SOCKET), *args[1:]])
        commands.append([binary, args[0], "--bridge-socket", str(BRIDGE_SOCKET), *args[1:]])
        commands.append([binary, args[0], "--no-remote", "--bridge-socket", str(BRIDGE_SOCKET), *args[1:]])
    commands.append([binary, *args])
    return commands


def run_peekaboo_variants(commands: Iterable[list[str]], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    errors: list[str] = []
    for cmd in commands:
        try:
            return run(cmd, timeout=timeout)
        except Exception as exc:  # pragma: no cover - exercised only on macOS host
            errors.append(str(exc))
    raise RuntimeError(" ; ".join(errors) or "peekaboo command failed")
