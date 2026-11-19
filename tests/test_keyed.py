"""The programmatic surface: ``send``/``result`` by key, for a plugin backend with no calling conversation.

One key is one task and one session; a later send on the key is a follow-up in that session; the final answer is
kept for ``result`` and nothing is injected anywhere. The tool's task list and its injections stay as they were.
The target gateway is replaced by a fake ``_send`` (the e2e trial ``keyed`` runs the real one).

Run: ``PT_SCRIPT=tests/test_keyed.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes interpreter).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "profile-tasks" / "__init__.py"


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

    def get_config(self, key, default=None):
        return default

    def inject_message(self, text, session_key=None):
        self.injected.append((text, session_key))
        return True


def main() -> int:
    mod = load()
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    def raises(exc_type, fn, *args, **kwargs) -> str | None:
        try:
            fn(*args, **kwargs)
        except exc_type as exc:
            return str(exc)
        return None

    refused = raises(RuntimeError, mod.send, "k", "analyst", "x")
    check(refused == "profile-tasks is not loaded in this process", "send before the plugin is registered is "
          "refused", error=refused)

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])
    mod._PLUGIN = plugin
    plugin.targets = lambda: ["analyst", "reviewer"]
    sent: list[tuple[str, str, bool]] = []
    me = mod._process_token(os.getpid())

    def fake_send(task_id, text, *, create):
        sent.append((task_id, text, create))
        task = plugin.store.get(task_id)
        session_id = task.get("session_id") or f"s-{task_id}"
        plugin.store.update(task_id, session_id=session_id, status="running", admission_id=f"a{len(sent)}",
                            turns=list(task.get("turns") or []) + [f"a{len(sent)}"], waiter=me)
        return {"task_id": task_id, "accepted": True, "profile": task["profile"], "session_id": session_id,
                "session_title": task["title"], "link": f"hermes://open/{session_id}", "note": "finish your turn"}

    plugin._send = fake_send
    plugin._running_turn = lambda task: None

    first = mod.send("pr-1", "reviewer", "Q1", title="PR one")
    second = mod.send("pr-2", "reviewer", "Q2")
    t1, t2 = plugin.store.get(first["task_id"]), plugin.store.get(second["task_id"])
    check(first["accepted"] and second["accepted"] and first["session_id"] != second["session_id"]
          and t1["key"] == "pr-1" and t2["key"] == "pr-2" and t1["caller_session_key"] is None
          and t1["title"].endswith(": PR one") and "note" not in first and first["key"] == "pr-1"
          and [c for _, _, c in sent] == [True, True],
          "two keys start two tasks, each with its own new session, no calling conversation and no tool note",
          first=first, second=second)

    plugin._deliver(first["task_id"], "Answer from reviewer ...", status="answered", last_answer="A1")
    got = mod.result("pr-1")
    check(got["status"] == "answered" and got["answer"] == "A1" and got["session_id"] == first["session_id"]
          and got["task_id"] == first["task_id"] and not ctx.injected,
          "the final answer is returned by result and injected nowhere", result=got, injected=ctx.injected)
    notices = plugin.store.get(first["task_id"]).get("notices") or []
    check(len(notices) == 1 and notices[0]["delivered_at"] is None and not plugin.store.get(first["task_id"]).get(
          "outbox"), "the answer notice is recorded on the task, never queued for injection", notices=notices)

    again = mod.send("pr-1", "reviewer", "Q1 follow-up")
    t1 = plugin.store.get(first["task_id"])
    check(again["task_id"] == first["task_id"] and again["session_id"] == first["session_id"]
          and sent[-1] == (first["task_id"], "Q1 follow-up", False) and len(t1["turns"]) == 2
          and t1.get("last_answer") is None and t1["caller_session_key"] is None,
          "a later send on the same key is a follow-up in the same session; the previous answer is cleared",
          again=again, turns=t1["turns"])

    started = time.monotonic()
    waited = mod.result("pr-1", wait=1.5)
    elapsed = round(time.monotonic() - started, 2)
    check(waited["status"] == "running" and "answer" not in waited and 1.4 <= elapsed < 4,
          "result with wait blocks while the turn runs, then returns its state without an answer",
          result=waited, elapsed=elapsed)

    errors = {
        "other profile": raises(ValueError, mod.send, "pr-1", "analyst", "x"),
        "empty key": raises(ValueError, mod.send, " ", "reviewer", "x"),
        "unknown profile": raises(ValueError, mod.send, "pr-3", "nope", "x"),
        "empty text": raises(ValueError, mod.send, "pr-4", "reviewer", "  "),
        "unknown key": raises(ValueError, mod.result, "pr-404"),
    }
    plugin._running_turn = lambda task: "turn started"
    errors["turn running"] = raises(ValueError, mod.send, "pr-2", "reviewer", "x")
    plugin._running_turn = lambda task: None
    check(all(errors.values()) and "belongs to profile reviewer" in errors["other profile"]
          and not plugin.task_for_key("pr-3") and not plugin.task_for_key("pr-4"),
          "refused calls raise ValueError and create no task", errors=errors)

    # A key whose task never got a session starts over with a new task.
    plugin.store.update("t-dead", task_id="t-dead", profile="reviewer", key="pr-5", caller_session_key=None,
                        title="Task t-dead", created_at=time.time() - 5, status="failed", session_id=None, turns=[])
    fresh = mod.send("pr-5", "reviewer", "Q5")
    check(fresh["task_id"] != "t-dead" and plugin.task_for_key("pr-5")["task_id"] == fresh["task_id"],
          "a key whose task never got a session starts a new task", fresh=fresh)

    tool = plugin.store.update("t-tool", task_id="t-tool", profile="analyst", caller_session_key="agent:main:x",
                               title="Task t-tool", created_at=time.time(), status="answered", session_id="s-t",
                               turns=["a"], last_answer="T")
    listed = [r["task_id"] for r in plugin.status(None)["tasks"]]
    plugin._deliver("t-tool", "Answer from analyst for task t-tool", status="answered", last_answer="T")
    check(listed == ["t-tool"] and ctx.injected == [("Answer from analyst for task t-tool", "agent:main:x")],
          "the tool's status list leaves keyed tasks out; a tool task's answer is still injected into its caller",
          listed=listed, injected=ctx.injected, tool=bool(tool))

    # A restart cuts off a keyed task: its notice is recorded, not injected.
    plugin.store.update(second["task_id"], waiter="999999999:0", status="running")
    ctx.injected.clear()
    found = plugin.recover_after_restart()
    time.sleep(0.2)
    plugin._flush_task(second["task_id"], "load")
    t2 = plugin.store.get(second["task_id"])
    check(second["task_id"] in found and not t2.get("outbox") and not ctx.injected
          and any(n["kind"] == "restart" for n in t2.get("notices") or []) and t2["status"] == "wait_lost",
          "a keyed task cut off by a restart gets its notice recorded, never injected", found=found,
          notices=t2.get("notices"), status=t2["status"])

    print("FAILURES", failures, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
