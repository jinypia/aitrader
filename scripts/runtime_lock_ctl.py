#!/usr/bin/env python3
"""Inspect and stop active bot runtime lock holders safely."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def read_lock_text(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def parse_pid_from_text(text: str) -> int | None:
    match = re.search(r"(?:^|\\s)pid=(\\d+)(?:\\s|$)", text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def get_cmd(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return ""


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lsof_holders(path: Path) -> list[int]:
    if shutil.which("lsof") is None:
        return []
    try:
        out = subprocess.check_output(
            ["lsof", "-t", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return sorted({int(line.strip()) for line in out.splitlines() if line.strip().isdigit()})
    except Exception:
        return []


def status(path: Path) -> int:
    print(f"lock_path={path}")
    print(f"exists={path.exists()}")
    text = read_lock_text(path)
    print(f"metadata={text or '(empty)'}")

    holders = lsof_holders(path)
    if holders:
        print("holders:")
        for pid in holders:
            cmd = get_cmd(pid)
            print(f"- pid={pid} cmd={cmd or '(unknown)'}")
    else:
        pid = parse_pid_from_text(text)
        if pid and pid_is_alive(pid):
            print("holders:")
            print(f"- pid={pid} cmd={get_cmd(pid) or '(unknown)'} (from lock metadata)")
        else:
            print("holders=(none)")
    return 0


def wait_for_exit(pid: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_is_alive(pid)


def stop(path: Path, timeout_s: float, allow_any: bool) -> int:
    text = read_lock_text(path)
    pids = lsof_holders(path)
    if not pids:
        pid = parse_pid_from_text(text)
        if pid and pid_is_alive(pid):
            pids = [pid]

    if not pids:
        print("No active lock holder process found.")
        return 0

    exit_code = 0
    for pid in pids:
        cmd = get_cmd(pid)
        safe_target = ("main.py" in cmd) or ("src/main.py" in cmd)
        if not safe_target and not allow_any:
            print(f"Skipping pid={pid}; command does not look like bot runtime: {cmd or '(unknown)'}")
            print("Re-run with --allow-any to force-stop this PID.")
            exit_code = 2
            continue

        print(f"Stopping pid={pid} cmd={cmd or '(unknown)'}")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print(f"Failed to signal pid={pid}: {exc}")
            exit_code = 1
            continue

        if wait_for_exit(pid, timeout_s):
            print(f"Stopped pid={pid} gracefully.")
            continue

        print(f"pid={pid} still running after {timeout_s:.1f}s; sending SIGKILL.")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as exc:
            print(f"Failed to SIGKILL pid={pid}: {exc}")
            exit_code = 1
            continue

        if wait_for_exit(pid, 2.0):
            print(f"Stopped pid={pid} with SIGKILL.")
        else:
            print(f"pid={pid} is still running after SIGKILL.")
            exit_code = 1

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Control AITRADER runtime lock holder.")
    parser.add_argument(
        "action",
        choices=["status", "stop"],
        help="Show lock owner status or stop active lock holder process",
    )
    parser.add_argument(
        "--lock-path",
        default="data/bot_runtime.lock",
        help="Lock file path (default: data/bot_runtime.lock)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for graceful stop before SIGKILL (default: 8)",
    )
    parser.add_argument(
        "--allow-any",
        action="store_true",
        help="Allow stopping PIDs even if command is not main.py",
    )

    args = parser.parse_args()
    lock_path = Path(args.lock_path)

    if args.action == "status":
        return status(lock_path)
    return stop(lock_path, timeout_s=max(0.5, float(args.timeout)), allow_any=bool(args.allow_any))


if __name__ == "__main__":
    raise SystemExit(main())
