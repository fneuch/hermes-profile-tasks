"""What reaches the calling conversation, notice by notice, through the plugin's own producers:

- not sent: the update of a turn that ends paused (``Not finished ... waiting on``), the wait-limit notice
  (``no answer within N minutes``), and the wait-limit notice of an earlier turn; status still shows them;
  a refused notice queued earlier is still retried at that event;
- still sent: the final answer, a failed turn, a failed turn while paused, an approval request, an MCP
  re-authentication notice, a cut wait, a restart, and the follow limit of a paused session.

Run: ``PT_SCRIPT=tests/test_quiet_progress.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
KEY = "agent:main:local:dm:manager"


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
        self.injected: list[tuple[str, str | None]] = []
        self.config: dict = {}
        self.accept = True

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def inject_message(self, text, session_key=None):
        if not self.accept:
            return False
        self.injected.append((text, session_key))
        return True


class WaitConn:
    """A target connection whose turn never ends before the wait limit."""

    def __init__(self, home_, on_frame=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def rpc(self, method, **params):
        if method == "prompt.submit":
            return {"admission_id": "adm-w"}
        if method == "session.info":
            return {"mcp_servers": []}
        return {}

    async def wait_terminal(self, session_id, admission_id, deadline):
        raise TimeoutError


def main() -> int:
    mod = load()
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:900], flush=True)

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])
    mod._PLUGIN = plugin

    def make(task_id: str, **fields) -> None:
        # A live waiter: status reads the stored task instead of the target gateway.
        plugin.store.update(task_id, **{
            "task_id": task_id, "profile": "implementer", "session_id": f"s-{task_id}", "caller_session_key": KEY,
            "status": "running", "turns": ["adm-1"], "admission_id": "adm-1", "turn_token": "tok-1",
            "waiter": mod._process_token(os.getpid()), "created_at": time.time(), "title": task_id, **fields})

    def observer() -> types.SimpleNamespace:
        return types.SimpleNamespace(ended=set(), conn=None, deadline=None, stop=lambda: None)

    def turn_end(task_id: str, text: str, status: str = "complete", error: str | None = None) -> None:
        payload = {"admission_id": "adm-1", "status": status, "text": text, **({"error": error} if error else {})}
        asyncio.run(plugin._turn_ended(task_id, {"session_id": f"s-{task_id}", "payload": payload}, observer()))

    def new_texts(n: int) -> list[str]:
        return [t for t, _ in ctx.injected[n:]]

    # Not sent: a turn that ends paused on WAITING:.
    make("t-upd")
    n = len(ctx.injected)
    turn_end("t-upd", "step one done\nWAITING: CI")
    row = plugin.status("t-upd")["tasks"][0]
    task = plugin.store.get("t-upd")
    check(not new_texts(n) and not task.get("outbox") and task["status"] == "waiting_subwork"
          and row.get("update") == "step one done\nWAITING: CI" and row.get("waiting_on") == ["CI"],
          "a paused turn's update is not sent to the manager; status shows it", injected=new_texts(n), row=row)

    # Not sent: the wait limit of the submitted turn.
    make("t-wait")
    real_conn = mod._Connection
    mod._Connection = WaitConn
    ctx.config["wait_timeout_seconds"] = 0.1
    try:
        n = len(ctx.injected)
        asyncio.run(plugin._send_and_wait("t-wait", "go", False, {}, threading.Event()))
    finally:
        mod._Connection = real_conn
        ctx.config.pop("wait_timeout_seconds")
    task = plugin.store.get("t-wait")
    check(not new_texts(n) and not task.get("outbox") and task["status"] == "waiting_timeout",
          "the wait-limit notice is not sent to the manager; the task is waiting_timeout", injected=new_texts(n),
          status=task["status"])

    # Not sent: the same two about an earlier turn, once a newer turn owns the task.
    make("t-old", turn_token="tok-2")
    n = len(ctx.injected)
    plugin._deliver("t-old", "no answer within 30 minutes", status="waiting_timeout", kind="wait_limit",
                    turn="tok-1", tell=False)
    check(not new_texts(n) and not plugin.store.get("t-old").get("outbox")
          and plugin.store.get("t-old")["status"] == "running",
          "an earlier turn's wait-limit notice is not sent and the newer turn's state stays", injected=new_texts(n))

    # Sent: the final answer.
    make("t-ans")
    n = len(ctx.injected)
    turn_end("t-ans", "all done")
    check(len(new_texts(n)) == 1 and new_texts(n)[0].startswith("Answer from implementer for task t-ans")
          and "all done" in new_texts(n)[0], "the final answer is sent", injected=new_texts(n))

    # Sent: a failed turn.
    make("t-fail")
    n = len(ctx.injected)
    turn_end("t-fail", "", status="error", error="model crashed")
    check(len(new_texts(n)) == 1 and "Task t-fail (implementer) did not complete: model crashed" in new_texts(n)[0],
          "a failed turn is sent", injected=new_texts(n))

    # Sent: a failed turn while the task is paused.
    make("t-pfail")
    n = len(ctx.injected)
    turn_end("t-pfail", "half\nWAITING: CI", status="error", error="tool crashed")
    check(len(new_texts(n)) == 1 and "this turn failed: tool crashed" in new_texts(n)[0]
          and plugin.store.get("t-pfail")["status"] == "waiting_subwork",
          "a failed turn of a paused task is sent", injected=new_texts(n))

    # Sent: an approval request.
    make("t-appr")
    n = len(ctx.injected)
    plugin._on_target_frame("t-appr", {"type": "approval.request", "session_id": "s-t-appr", "payload": {
        "prompt_id": "p1", "command": "rm -rf build", "description": "delete", "timeout_s": 60}})
    check(len(new_texts(n)) == 1 and "is waiting for an approval" in new_texts(n)[0]
          and "rm -rf build" in new_texts(n)[0], "an approval request is sent", injected=new_texts(n))

    # Sent: an MCP re-authentication notice.
    make("t-mcp")
    n = len(ctx.injected)
    plugin._on_target_frame("t-mcp", {"type": "tool.complete", "session_id": "s-t-mcp", "payload": {
        "tool_id": "c1", "name": "mcp__linear__x", "is_error": True,
        "result": {"error": "re-auth", "needs_reauth": True, "server": "linear"}}})
    check(len(new_texts(n)) == 1 and "hermes -p implementer mcp login linear" in new_texts(n)[0],
          "an MCP re-authentication notice is sent", injected=new_texts(n))

    # Sent: a cut wait.
    make("t-cut")
    n = len(ctx.injected)
    plugin._wait_cut("t-cut", None, "Gateway disconnected")
    check(len(new_texts(n)) == 1 and "the wait for the answer was cut off" in new_texts(n)[0],
          "a cut wait is sent", injected=new_texts(n))

    # Sent: a restart that cut a waiting task.
    make("t-rst", waiter="999999999:0")
    n = len(ctx.injected)
    found = plugin.recover_after_restart()
    plugin._flush_task("t-rst", "load")
    check("t-rst" in found and any("the gateway restarted" in t and "t-rst" in t for t in new_texts(n)),
          "a task cut by a restart is sent", found=found, injected=new_texts(n))

    # Sent: the follow limit of a paused session.
    make("t-lim", status="waiting_subwork", pause_reasons=["CI"])
    n = len(ctx.injected)
    plugin._follow_limit("t-lim", observer())
    check(len(new_texts(n)) == 1 and "no news for" in new_texts(n)[0] and "waiting on CI" in new_texts(n)[0],
          "the follow limit of a paused session is sent", injected=new_texts(n))

    # A paused turn's end still retries a notice queued earlier and refused, and sends only that one.
    make("t-retry")
    ctx.accept = False
    plugin._notify("t-retry", "earlier refused notice", "approval")
    ctx.accept = True
    n = len(ctx.injected)
    turn_end("t-retry", "half\nWAITING: CI")
    check(new_texts(n) == ["earlier refused notice"] and not plugin.store.get("t-retry").get("outbox"),
          "a paused turn's end retries a refused earlier notice without adding its update", injected=new_texts(n))

    print(f"FAILURES {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
