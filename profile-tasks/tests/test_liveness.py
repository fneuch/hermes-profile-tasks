"""Waiter liveness on both code paths: Linux /proc, and no /proc (macOS, simulated here by hiding /proc).

A live waiter is alive; the same pid with another start time, a pid that is gone, and a zombie are not.

Run: ``PT_SCRIPT=tests/test_liveness.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter). ``e2e/run.sh <path to __init__.py>`` with that script runs it against another copy of the plugin.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "__init__.py")


def load():
    spec = importlib.util.spec_from_file_location("profile_tasks_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def without_proc():
    """Make /proc look absent, as on macOS."""
    real_exists, real_read = pathlib.Path.exists, pathlib.Path.read_text

    def exists(self, *a, **k):
        return False if str(self).startswith("/proc/") else real_exists(self, *a, **k)

    def read_text(self, *a, **k):
        if str(self).startswith("/proc/"):
            raise FileNotFoundError(str(self))
        return real_read(self, *a, **k)

    pathlib.Path.exists, pathlib.Path.read_text = exists, read_text
    try:
        yield
    finally:
        pathlib.Path.exists, pathlib.Path.read_text = real_exists, real_read


def main() -> int:
    mod = load()
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)  # exited, not reaped: a zombie

    for path, scope in (("/proc", contextlib.nullcontext), ("no /proc", without_proc)):
        with scope():
            token = mod._process_token(os.getpid())
            pid, start = token.split(":", 1)
            check(bool(start) and mod._waiter_alive(token), f"{path}: a live waiter is alive", token=token)
            check(not mod._waiter_alive(f"{pid}:{start}0"), f"{path}: the same pid with another start time is not")
            check(not mod._waiter_alive(f"{gone.pid}:1"), f"{path}: a pid that is gone is not alive")
            zombie_token = mod._process_token(zombie.pid)
            check(not mod._waiter_alive(zombie_token), f"{path}: a zombie waiter is not alive", token=zombie_token)
    zombie.wait()

    print(f"{failures} failure(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
