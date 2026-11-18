"""Restart recovery when several processes load the plugin at once (the launcher that attaches to the host
gateway, the host gateway itself): each cut-off task gets exactly one restart notice, its open approval ends
as ``interrupted`` and no stale waiter is left.

Run: ``PT_SCRIPT=tests/test_restart_race.py e2e/run.sh [rounds] [processes] [plugin __init__.py]``
(isolated HOME/HERMES_HOME, the installed Hermes interpreter).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The loading processes read the plugin path from PT_PLUGIN, set from the third argument.
PLUGIN = Path(os.environ.get("PT_PLUGIN") or Path(__file__).resolve().parents[1] / "profile-tasks" / "__init__.py")


def load():
    spec = importlib.util.spec_from_file_location("profile_tasks_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    profile_name = "default"
    _manager = None

    def __init__(self):
        from hermes_cli.plugins_state import PluginState
        self.state = PluginState("profile-tasks")

    def get_config(self, key, default=None):
        return default

    def inject_message(self, text, session_key=None):
        return False


def child(start_at: float) -> None:
    mod = load()
    plugin = mod.ProfileTasks(FakeCtx(), [])
    time.sleep(max(0.0, start_at - time.time()))
    plugin.recover_after_restart()


def seed(ctx: FakeCtx, dead: str) -> dict:
    """Tasks a stopped gateway leaves behind, among them the failing shape (wait_lost + watch_lost)."""
    approval = {"prompt_id": "p1", "execution_generation": 1, "command": "rm -rf probe", "description": "delete",
                "choices": ["once", "deny"], "timeout_s": 10.0, "raised_at": time.time(),
                "expires_at": time.time() + 10}
    base = {"profile": "analyst", "caller_session_key": "agent:main:local:dm:x", "waiter": dead, "turns": ["a"],
            "outbox": [], "notices": []}
    tasks = {
        "t-wait": base | {"task_id": "t-wait", "status": "running", "session_id": "s1", "admission_id": "a"},
        "t-pending": base | {"task_id": "t-pending", "status": "running", "session_id": "s2", "admission_id": "a",
                             "pending_approval": approval},
        "t-watchlost": base | {"task_id": "t-watchlost", "status": "wait_lost", "session_id": "s3",
                               "admission_id": "a", "error": "GatewayClientError: Gateway disconnected",
                               "last_approval": approval | {"outcome": "watch_lost"},
                               "outbox": [{"id": "w1", "kind": "wait_lost", "text": "cut", "queued_at": 0,
                                           "attempts": []}]},
        # The stopping gateway withdrew the approval (cancelled) just before it cut the wait.
        "t-stopcancel": base | {"task_id": "t-stopcancel", "status": "wait_lost", "session_id": "s5",
                                "admission_id": "a", "error": "GatewayClientError: Gateway disconnected",
                                "last_approval": approval | {"outcome": "cancelled", "settled_after_s": 1.0},
                                "notices": [{"id": "w2", "kind": "wait_lost",
                                             "queued_at": approval["raised_at"] + 1.05, "attempts": []}]},
        # A correction withdrew the approval long before the stop: it keeps its outcome.
        "t-steered": base | {"task_id": "t-steered", "status": "running", "session_id": "s6", "admission_id": "a",
                             "last_approval": approval | {"raised_at": time.time() - 600, "outcome": "cancelled",
                                                          "settled_after_s": 1.0}},
        "t-never": base | {"task_id": "t-never", "status": "starting", "session_id": None, "turns": []},
        "t-done": base | {"task_id": "t-done", "status": "answered", "session_id": "s4", "admission_id": "a"},
    }
    ctx.state.set("tasks", tasks)
    return tasks


def verify(tasks: dict) -> list[str]:
    errors = []
    for task_id in ("t-wait", "t-pending", "t-watchlost", "t-stopcancel", "t-steered", "t-never"):
        task = tasks[task_id]
        restarts = [e for e in task.get("outbox") or [] if e["kind"] == "restart"]
        if len(restarts) != 1:
            errors.append(f"{task_id}: {len(restarts)} restart notices")
        if task.get("waiter") is not None:
            errors.append(f"{task_id}: stale waiter {task.get('waiter')}")
        if task.get("pending_approval") or task.get("settling_approval"):
            errors.append(f"{task_id}: approval still open")
    for task_id in ("t-pending", "t-watchlost", "t-stopcancel"):
        last = tasks[task_id].get("last_approval") or {}
        text = "".join(e["text"] for e in tasks[task_id].get("outbox") or [] if e["kind"] == "restart")
        if last.get("outcome") != "interrupted":
            errors.append(f"{task_id}: approval outcome {last.get('outcome')}")
        if "did not run" not in text:
            errors.append(f"{task_id}: restart notice does not say the command did not run")
    if [e["id"] for e in tasks["t-watchlost"]["outbox"] if e["kind"] == "wait_lost"] != ["w1"]:
        errors.append("t-watchlost: the earlier wait_lost notice was lost")
    if (tasks["t-steered"].get("last_approval") or {}).get("outcome") != "cancelled":
        errors.append("t-steered: an approval withdrawn long before the stop was relabelled")
    if tasks["t-done"].get("outbox"):
        errors.append("t-done: an answered task got a notice")
    if [s for s in (tasks[t].get("status") for t in ("t-wait", "t-pending", "t-watchlost")) if s != "wait_lost"]:
        errors.append("a waiting task is not wait_lost")
    if tasks["t-never"].get("status") != "failed":
        errors.append("t-never: not failed")
    return errors


def main(argv: list[str]) -> int:
    if argv[:1] == ["--child"]:
        child(float(argv[1]))
        return 0
    rounds = int(argv[0]) if argv else 50
    procs = int(argv[1]) if len(argv) > 1 else 3
    if len(argv) > 2:
        os.environ["PT_PLUGIN"] = str(Path(argv[2]).resolve())
    print("plugin:", os.environ.get("PT_PLUGIN") or PLUGIN, flush=True)
    ctx = FakeCtx()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    dead = f"{gone.pid}:0"
    failed = 0
    for n in range(rounds):
        seed(ctx, dead)
        start_at = time.time() + 1.5
        children = [subprocess.Popen([sys.executable, __file__, "--child", str(start_at)]) for _ in range(procs)]
        for proc in children:
            proc.wait(60)
        errors = verify(ctx.state.get("tasks"))
        failed += bool(errors)
        print(f"round {n + 1}: {'FAIL ' + json.dumps(errors) if errors else 'ok'}", flush=True)
    print(f"{rounds - failed}/{rounds} rounds passed with {procs} concurrent loads")
    print("PASS" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
