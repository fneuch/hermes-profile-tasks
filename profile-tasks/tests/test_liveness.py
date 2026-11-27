"""Waiter liveness on every code path: Linux /proc, and no /proc (macOS, simulated here by hiding /proc; on Windows
this is the only path, read with psutil).

A live waiter is alive; the same pid with another start time, a pid that is gone, and a zombie (where the
platform has them) are not. On Windows the check never calls ``os.kill``: signal 0 there is CTRL_C_EVENT.

The tokens of the ended processes are the real ones, read while the process was alive: a made-up token such as
``<pid>:1`` is refused by its start time alone, so it would pass even if an ended process read as alive.

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
import time
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

    def ended_waiter(release_handle: bool) -> dict:
        """A process that ended after its token was read: the token is the real one, read while it was alive.

        With *release_handle* False this process keeps the handle of the ended process (Windows lists such a
        process without a state of its own); True closes it, so the pid is gone for good.
        """
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        token = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            token = mod._process_token(proc.pid)
            if token.split(":", 1)[1]:
                break
            time.sleep(0.05)
        alive_before = mod._waiter_alive(token)
        proc.kill()
        proc.wait(30)
        if release_handle and getattr(proc, "_handle", None) is not None:
            proc._handle.Close()  # Windows only: POSIX Popen has no handle
        return {"proc": proc, "token": token, "alive_before": alive_before}

    zombie = None
    if hasattr(os, "waitid"):
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)  # exited, not reaped: a zombie

    scopes = [("no /proc", without_proc)]
    if os.name != "nt":
        scopes.insert(0, ("/proc", contextlib.nullcontext))
    gone_token = ""
    for path, scope in scopes:
        with scope():
            token = mod._process_token(os.getpid())
            pid, start = token.split(":", 1)
            check(bool(start) and mod._waiter_alive(token), f"{path}: a live waiter is alive", token=token)
            check(not mod._waiter_alive(f"{pid}:{start}0"), f"{path}: the same pid with another start time is not")
            # Tokens are read inside the scope: /proc and psutil format the start time differently.
            for what, release in (("a pid that is gone", True), ("an exited waiter, its handle held", False)):
                ended = ended_waiter(release)
                check(ended["alive_before"] and not mod._waiter_alive(ended["token"]),
                      f"{path}: {what} is not alive (real token, read while it was alive)",
                      token=ended["token"], alive_before=ended["alive_before"])
                if release:
                    gone_token = ended["token"]
            if zombie is not None:
                zombie_token = mod._process_token(zombie.pid)
                check(not mod._waiter_alive(zombie_token), f"{path}: a zombie waiter is not alive", token=zombie_token)
    if zombie is not None:
        zombie.wait()

    if os.name == "nt":
        # os.kill would send CTRL_C_EVENT to a process group there: the check must not call it at all.
        calls = []
        real_kill = os.kill
        os.kill = lambda *args: calls.append(args)
        try:
            alive_and_gone = (mod._waiter_alive(mod._process_token(os.getpid())), mod._waiter_alive(gone_token))
        finally:
            os.kill = real_kill
        check(alive_and_gone == (True, False) and calls == [],
              "Windows: alive and gone waiters are told apart without os.kill",
              alive_and_gone=alive_and_gone, calls=calls)

    print(f"{failures} failure(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
