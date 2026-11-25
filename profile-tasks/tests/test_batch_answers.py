"""The final answers of one conversation's tasks go out together, with a fake calling conversation:

- answers and failures wait while another task of the conversation runs, waits or is paused, then go in one
  injection; a task of another conversation holds nothing;
- a task started while others run joins the wait;
- past the batch limit (15 minutes) what is ready goes out even while a task still runs, so a second message follows;
- approvals, approval ends and MCP sign-in notices go at once and do not carry the held answers;
- a turn whose last line is ``BLOCKED:`` goes at once, recorded answered with what it needs; mid-text it does not;
- a held answer taken by status is not injected again;
- held answers survive a reload and go out at load once nothing is open; a refused batch is kept whole;
- the target is told about the ``BLOCKED:`` line;
- a held answer still goes to its conversation after a follow_up from another one;
- a held answer of an earlier turn is not lost when status takes the newer turn's.

Run: ``PT_SCRIPT=tests/test_batch_answers.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

PLUGIN = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "__init__.py")


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
        self.accept = True

    def get_config(self, key, default=None):
        return default

    def inject_message(self, text, session_key=None):
        if not self.accept:
            return False
        self.injected.append((text, session_key))
        return True


class FakeObserver:
    def __init__(self):
        self.ended: set[str] = set()
        self.conn = None
        self.deadline = None
        self.stopped = False

    def stop(self):
        self.stopped = True


def main() -> int:
    mod = load()
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])

    def task(task_id: str, status: str = "running", caller: str = "mgr", **fields) -> None:
        plugin.store.update(task_id, **{
            "task_id": task_id, "profile": "analyst", "title": f"Task {task_id}", "status": status,
            "session_id": f"s-{task_id}", "admission_id": "a1", "turns": ["a1"], "caller_session_key": caller,
            "waiter": None, "turn_token": "t1", **fields})

    def answer(task_id: str, text: str | None = None) -> None:
        plugin._deliver(task_id, text or f"Answer for {task_id}", status="answered", last_answer=text or task_id)

    def fail(task_id: str) -> None:
        plugin._deliver(task_id, f"Task {task_id} did not complete: boom", status="failed", error="boom")

    def sent_since(before: int) -> list[tuple[str, str | None]]:
        return ctx.injected[before:]

    def outbox(task_id: str) -> list[dict]:
        return list((plugin.store.get(task_id) or {}).get("outbox") or [])

    # 1. Three tasks of one conversation and one of another: the conversation's answers go in one injection.
    for tid in ("g1", "g2", "g3"):
        task(tid)
    task("other", caller="mgr-2")
    before = len(ctx.injected)
    answer("g1")
    fail("g2")
    held = sent_since(before)
    answer("g3")
    batch = sent_since(before)
    check(held == [] and len(batch) == 1 and batch[0][1] == "mgr"
          and all(f"Answer for {t}" in batch[0][0] for t in ("g1", "g3")) and "Task g2 did not complete" in batch[0][0]
          and batch[0][0].index("g1") < batch[0][0].index("g2") < batch[0][0].index("Answer for g3")
          and all(plugin.store.get(t).get("delivered") is True and not outbox(t) for t in ("g1", "g2", "g3")),
          "an answer and a failure wait while a task of the conversation runs; the last end sends all three in one "
          "injection, oldest first, and each is marked delivered; a running task of another conversation holds "
          "nothing", held=held, batch=batch)

    # 2. Waiting past the wait limit and paused tasks hold the answers too.
    for status in ("waiting_timeout", "waiting_subwork", "starting"):
        task(f"open-{status}", status=status)
        task(f"done-{status}")
        before = len(ctx.injected)
        answer(f"done-{status}")
        check(sent_since(before) == [] and len(outbox(f"done-{status}")) == 1,
              f"a task {status} holds the conversation's answers", injected=sent_since(before))
        answer(f"open-{status}")
        sent = sent_since(before)
        check(len(sent) == 1 and f"Answer for done-{status}" in sent[0][0] and f"Answer for open-{status}" in sent[0][0],
              f"the {status} task's own end sends both answers in one injection", sent=sent)

    # 3. A task started while the others run joins the wait.
    task("early")
    task("held")
    before = len(ctx.injected)
    answer("held")
    task("late-start", status="starting")
    answer("early")
    waited = sent_since(before)
    plugin.store.update("late-start", status="running")
    answer("late-start")
    sent = sent_since(before)
    check(waited == [] and len(sent) == 1 and all(f"Answer for {t}" in sent[0][0] for t in ("held", "early", "late-start")),
          "a task started while the answers wait keeps them waiting; its end sends the three together",
          waited=waited, sent=sent)

    # 4. The batch limit: what is ready goes after 15 minutes even while a task still runs.
    check(mod.BATCH_LIMIT_SECONDS == 900 and plugin.batch_seconds == 900, "the batch limit is 15 minutes",
          limit=mod.BATCH_LIMIT_SECONDS)
    task("slow")
    task("quick")
    before = len(ctx.injected)
    answer("quick")

    def age(task_id: str, seconds: float) -> None:
        plugin.store.modify(task_id, lambda t: {"outbox": [e | {"queued_at": time.time() - seconds}
                                                           for e in t.get("outbox") or []]})

    age("quick", 899)
    plugin._flush_outbox("retry")
    early = sent_since(before)
    age("quick", 901)
    plugin._flush_outbox("retry")
    capped = sent_since(before)
    answer("slow")
    sent = sent_since(before)
    check(early == [] and len(capped) == 1 and "Answer for quick" in capped[0][0] and "slow" not in capped[0][0]
          and plugin.store.get("slow").get("status") == "answered" and len(sent) == 2
          and "Answer for slow" in sent[1][0] and plugin.store.get("quick").get("delivered") is True,
          "at 899 s the ready answer still waits; past 900 s the retry sends it alone while the other task runs, "
          "and the slow task's answer is a second message", early=early, capped=capped, sent=sent)

    # The real retry loop applies the limit by itself, without any event or tool call.
    task("slow-2")
    task("quick-2")
    before = len(ctx.injected)
    answer("quick-2")
    age("quick-2", 901)
    plugin.retry_seconds = 0.2
    plugin.ctx._manager = type("M", (), {"has_gateway_message_injector": True})()
    mod._PLUGIN = plugin
    import threading
    loop = threading.Thread(target=plugin._retry_loop, daemon=True)
    loop.start()
    end = time.monotonic() + 5
    while time.monotonic() < end and not sent_since(before):
        time.sleep(0.05)
    mod._PLUGIN = None
    loop.join(2)
    check(len(sent_since(before)) == 1 and "Answer for quick-2" in sent_since(before)[0][0],
          "the retry loop sends an answer past the batch limit by itself", sent=sent_since(before))
    plugin.ctx._manager = None
    answer("slow-2")

    # 5. Approvals, approval ends and MCP sign-in notices go at once, without the held answers.
    task("busy")
    task("ready")
    task("asking")
    before = len(ctx.injected)
    answer("ready")
    for kind in ("approval", "approval_end", "mcp_auth"):
        plugin._notify("asking", f"{kind} notice of asking", kind)
    sent = sent_since(before)
    check(len(sent) == 3 and all(f"{k} notice of asking" in s[0] for k, s in zip(("approval", "approval_end", "mcp_auth"), sent))
          and not any("Answer for ready" in s[0] for s in sent) and len(outbox("ready")) == 1,
          "an approval request, its end and an MCP sign-in notice each go at once; the held answer stays held",
          sent=sent)
    answer("busy")
    answer("asking")

    # 6. A turn whose last line is BLOCKED: goes at once, recorded answered with what it needs.
    task("running-b")
    task("blocker", session_id="s-blocker")
    before = len(ctx.injected)
    frame = {"session_id": "s-blocker", "type": "message.complete", "payload": {
        "admission_id": "a1", "status": "complete", "text": "Found two options.\nBLOCKED: pick option A or B"}}
    asyncio.run(plugin._turn_ended("blocker", frame, FakeObserver()))
    sent = sent_since(before)
    stored = plugin.store.get("blocker")
    check(len(sent) == 1 and "Blocked: answer from analyst for task blocker" in sent[0][0]
          and "it needs pick option A or B" in sent[0][0] and stored.get("status") == "answered"
          and stored.get("blocked") == "pick option A or B" and stored.get("delivered") is True,
          "a BLOCKED: last line is sent at once while another task runs, and the task is answered with what it needs",
          sent=sent, task={k: stored.get(k) for k in ("status", "blocked", "delivered")})
    task("not-blocker", session_id="s-not-blocker")
    frame = {"session_id": "s-not-blocker", "type": "message.complete", "payload": {
        "admission_id": "a1", "status": "complete", "text": "BLOCKED: was a problem earlier\nAll fixed now."}}
    before = len(ctx.injected)
    asyncio.run(plugin._turn_ended("not-blocker", frame, FakeObserver()))
    check(sent_since(before) == [] and plugin.store.get("not-blocker").get("blocked") is None
          and len(outbox("not-blocker")) == 1,
          "BLOCKED: anywhere but the last line is a plain answer that waits", sent=sent_since(before))
    answer("running-b")
    check(len(sent_since(before)) == 1 and "All fixed now." in sent_since(before)[0][0],
          "the plain answer goes with the last task's", sent=sent_since(before))

    # 7. A held answer taken by status is not injected again.
    task("still")
    task("asked")
    before = len(ctx.injected)
    answer("asked", "ASKED-ANSWER")
    row = plugin.status("asked")["tasks"][0]
    answer("still")
    sent = sent_since(before)
    check(row.get("answer") == "ASKED-ANSWER" and len(sent) == 1 and "ASKED-ANSWER" not in sent[0][0]
          and "Answer for still" in sent[0][0],
          "status hands over a held answer; the batch then carries only the other task's", row=row, sent=sent)

    # 8. Held answers survive a reload; a refused batch is kept whole.
    task("r-open")
    task("r-done")
    answer("r-done")
    reloaded = mod.ProfileTasks(ctx, [])
    before = len(ctx.injected)
    reloaded._flush_outbox("load")
    still = sent_since(before)
    reloaded.store.update("r-open", status="wait_lost")
    ctx.accept = False
    reloaded._flush_outbox("load")
    refused = (len(outbox("r-done")), len(outbox("r-open")))
    ctx.accept = True
    reloaded._flush_outbox("retry")
    sent = sent_since(before)
    check(still == [] and refused == (1, 0) and len(sent) == 1 and "Answer for r-done" in sent[0][0]
          and not outbox("r-done"),
          "after a reload a held answer still waits for an open task; once none is open it goes at load, and a "
          "refused batch is kept and sent whole at the retry", still=still, refused=refused, sent=sent)

    # 9. The target is told about the BLOCKED: line.
    check("`BLOCKED: <what you need>`" in mod.TARGET_GUIDANCE and "BLOCKED" in mod.SCHEMA["description"],
          "the text sent with every task and the tool description name the BLOCKED: line",
          guidance=mod.TARGET_GUIDANCE)

    # 10. A follow_up from another conversation does not take the answer held for the first one.
    from gateway.session_context import clear_session_vars, set_session_vars
    plugin._send = lambda task_id, text, *, create: {"task_id": task_id, "accepted": True}
    plugin._live_observer = lambda task_id: None
    task("a-open")
    task("a-done", admission_id=None)
    before = len(ctx.injected)
    answer("a-done", "A-ANSWER")
    tokens = set_session_vars(session_key="mgr-b", session_id="conv-b", source="desktop")
    try:
        plugin.follow_up("a-done", "more, for conversation B")
    finally:
        clear_session_vars(tokens)
    moved = sent_since(before)
    answer("a-open")
    sent = sent_since(before)
    check(moved == [] and plugin.store.get("a-done").get("caller_session_key") == "mgr-b"
          and len(sent) == 1 and sent[0][1] == "mgr" and "A-ANSWER" in sent[0][0] and "Answer for a-open" in sent[0][0],
          "an answer held for conversation A still goes to A, with A's other answers, after conversation B sends "
          "the task a follow_up", moved=moved, sent=sent)

    # 11. A held answer of an earlier turn is not lost when status takes the newer turn's answer.
    task("t-open")
    task("t-two", admission_id=None)
    before = len(ctx.injected)
    answer("t-two", "FIRST-TURN")
    plugin.follow_up("t-two", "second turn")
    answer("t-two", "SECOND-TURN")
    row = plugin.status("t-two")["tasks"][0]
    answer("t-open")
    sent = sent_since(before)
    check(row.get("answer") == "SECOND-TURN" and len(sent) == 1 and "FIRST-TURN" in sent[0][0]
          and "SECOND-TURN" not in sent[0][0],
          "status takes only the newer turn's answer; the earlier turn's held answer still goes in the batch",
          row=row, sent=sent)

    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
