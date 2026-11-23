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

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"


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
                "session_title": task["title"], "link": mod._session_ref(task["profile"], session_id), "note": "finish your turn"}

    plugin._send = fake_send
    plugin._running_turn = lambda task, read=None: None

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
          "outbox") and "text" not in notices[0] and "notices" not in got
          and plugin.store.get(first["task_id"]).get("delivered") is True,
          "the answer notice is recorded on the task without its text, never queued for injection; result marks "
          "the answer delivered", notices=notices, result=got)

    # The wait limit and an MCP sign-in problem reach the keyed caller through result.
    plugin._deliver(first["task_id"], "no answer within 30 minutes", status="waiting_timeout", kind="wait_limit")
    plugin._mcp_auth_notice(first["task_id"], "jira", "sign-in rejected")
    # Past the wait limit status reads the target turn; there is no target here.
    plugin._refresh = lambda task: {}
    got = mod.result("pr-1")
    del plugin._refresh
    kinds = [n["kind"] for n in got.get("notices") or []]
    check(kinds == ["wait_limit", "mcp_auth"] and "mcp login jira" in got["notices"][1]["text"]
          and all(set(n) == {"kind", "text", "queued_at"} for n in got["notices"]) and not ctx.injected,
          "result lists the wait limit and MCP re-authentication notices of a keyed task", result=got)

    for i in range(mod.KEYED_NOTICE_LIMIT + 5):
        plugin._notify(first["task_id"], f"notice {i}", "approval")
    kept = plugin.store.get(first["task_id"])["notices"]
    check(len(kept) == mod.KEYED_NOTICE_LIMIT and kept[-1]["text"] == f"notice {mod.KEYED_NOTICE_LIMIT + 4}",
          "a keyed task keeps only its latest notices", kept=len(kept), last=kept[-1].get("text"))
    plugin.store.update(first["task_id"], status="answered", last_answer="A1")

    again = mod.send("pr-1", "reviewer", "Q1 follow-up")
    t1 = plugin.store.get(first["task_id"])
    check(again["task_id"] == first["task_id"] and again["session_id"] == first["session_id"]
          and sent[-1] == (first["task_id"], "Q1 follow-up", False) and len(t1["turns"]) == 2
          and t1.get("last_answer") is None and t1["caller_session_key"] is None and not t1.get("notices"),
          "a later send on the same key is a follow-up in the same session; the previous answer and notices are "
          "cleared", again=again, turns=t1["turns"], notices=t1.get("notices"))

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
        "non-numeric wait": raises(ValueError, mod.result, "pr-1", wait="soon"),
    }
    plugin._running_turn = lambda task, read=None: "turn started"
    errors["turn running"] = raises(ValueError, mod.send, "pr-2", "reviewer", "x")
    plugin._running_turn = lambda task, read=None: None
    # A second send on a key whose first send is still being submitted (no session yet).
    plugin.store.update("t-start", task_id="t-start", profile="reviewer", key="pr-6", caller_session_key=None,
                        title="Task t-start", created_at=time.time(), status="starting", session_id=None, turns=[],
                        waiter=me)
    plugin._running_turn = mod.ProfileTasks._running_turn
    errors["still starting"] = raises(ValueError, mod.send, "pr-6", "reviewer", "x")
    plugin._running_turn = lambda task, read=None: None
    check(all(errors.values()) and "belongs to profile reviewer" in errors["other profile"]
          and "turn still running" in errors["still starting"] and "no session" not in errors["still starting"]
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
    restart = [n for n in t2.get("notices") or [] if n["kind"] == "restart"]
    text = restart[0]["text"] if restart else ""
    check(second["task_id"] in found and not t2.get("outbox") and not ctx.injected and restart
          and t2["status"] == "wait_lost" and f"Task {second['task_id']}" not in text and "profile_task" not in text
          and "injected" not in text and "result()" in text,
          "a keyed task cut off by a restart gets a neutral notice recorded, never injected", found=found,
          notices=t2.get("notices"), status=t2["status"])

    # Every notice a keyed task can get, from the plugin's own code paths.
    k7 = mod.send("pr-7", "reviewer", "Q7")["task_id"]
    plugin._approval_raised(k7, {"prompt_id": "p7", "command": "rm -rf x", "description": "recursive delete",
                                 "timeout_s": 10})
    plugin._approval_ended(k7, "p7", ("timeout", ""))
    plugin._mcp_auth_notice(k7, "jira7", "sign-in rejected")
    plugin._deliver(k7, mod._wait_limit_text(k7, plugin.store.get(k7), 30.0), status="waiting_timeout",
                    kind="wait_limit")
    plugin._approval_raised(k7, {"prompt_id": "p8", "command": "rm -rf y", "description": "recursive delete",
                                 "timeout_s": 10})

    async def cut_off(task_id, text, create, accepted, ready):
        ready.set()
        raise ConnectionError("connection reset")

    plugin._send_and_wait = cut_off
    plugin._run_send(k7, "x", False, {}, __import__("threading").Event())
    del plugin._send_and_wait
    plugin.store.update(k7, waiter="999999999:0", status="running",
                        pending_approval={"prompt_id": "p9", "command": "rm -rf z", "timeout_s": 10,
                                          "raised_at": time.time()})
    plugin.recover_after_restart()
    plugin._flush_task(k7, "load")
    task_ids = set(plugin.store.all())
    texts = []
    for t in plugin.store.all().values():
        if t.get("key"):
            for n in t.get("notices") or []:
                if n.get("text"):
                    texts.append((n["kind"], n["text"].replace(t.get("session_id") or "\0", "<session>")))
    leaks = [(kind, text) for kind, text in texts
             if "profile_task" in text or any(tid in text for tid in task_ids)]
    kinds7 = [n["kind"] for n in plugin.store.get(k7).get("notices") or []]
    check(not leaks and set(kinds7) >= {"approval", "approval_end", "mcp_auth", "wait_limit", "wait_lost", "restart"}
          and not ctx.injected,
          "no keyed notice text names profile_task or a task id (approval, its ending, MCP sign-in, wait limit, "
          "cut-off wait, restart)", leaks=leaks, kinds=kinds7, checked=len(texts))

    class UnloadCtx(FakeCtx):
        def __init__(self):
            super().__init__()
            self.unload = []

        def register_tool(self, **kw):
            pass

        def register_hook(self, name, callback):
            pass

        def on_unload(self, callback):
            self.unload.append(callback)

    mod._PLUGIN = None
    unload_ctx = UnloadCtx()
    mod.register(unload_ctx)
    loaded = mod._PLUGIN is not None
    for callback in unload_ctx.unload:
        callback()
    check(loaded and mod._PLUGIN is None, "unloading the plugin clears the plugin send() and result() use",
          callbacks=len(unload_ctx.unload))

    print("FAILURES", failures, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
