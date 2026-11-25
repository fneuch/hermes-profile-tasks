"""Turn and approval races, made deterministic with fakes for the target gateway:

- two follow_ups racing on one task start one turn only;
- a follow_up clears the previous turn's answer and error, keyed or not;
- an earlier turn's worker that ends late does not overwrite the newer turn's state, its answer held as a notice;
- an approval cut off by the end of its turn says the command outcome is unknown;
- an approval answer is marked forwarded only once the target took it, and a failed forward keeps the
  outcome notice;
- a tool call running beside the gated one does not end its approval;
- a status read of an earlier turn does not overwrite a newer turn's state;
- frames queued before the terminal receipt still reach the plugin;
- a protected-file write whose approval timed out, was denied or withdrawn is not reported as run;
- a late frame of an earlier turn leaves the newer turn's approval alone, and a follow_up waits for the frame
  being handled;
- a second approval card for the same request is refused;
- a status read that hands over the answer still lists the other notices not delivered;
- an answer to an approval the target had already ended is not reported as forwarded;
- two approvals open in one session each end on their own call;
- a worker failing after the acknowledgement wait records the task failed;
- a turn cut off with a call open leaves no stale call for the next turn's approval;
- an answered task is not reported cut off by a restart at the next load;
- no receipt is read while the key or send lock is held;
- approvals raised over the same calls never get a definite outcome, in either result order;
- a follow_up cut off by a restart before the target accepted it is not given the previous turn's answer.

Run: ``PT_SCRIPT=tests/test_turn_races.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter). ``e2e/run.sh <path to __init__.py>`` with that script runs it against another copy of the plugin.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
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

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])

    async def terminal_receipt(task):
        return {"status": "terminal"}

    plugin._read_receipt = terminal_receipt
    sent: list[str] = []

    def fake_send(task_id, text, *, create):
        # The real _send leaves the task 'starting' until the target accepts the text.
        sent.append(task_id)
        return {"task_id": task_id, "accepted": True}

    plugin._send = fake_send

    def answered_task(task_id: str, **fields) -> None:
        plugin.store.update(task_id, **{
            "task_id": task_id, "profile": "analyst", "title": f"Task {task_id}", "status": "answered",
            "session_id": f"s-{task_id}", "admission_id": "a1", "turns": ["a1"], "caller_session_key": "mgr",
            "waiter": None, "last_answer": "first answer", "turn_token": "t1", **fields})

    # 1. Both callers read the answered task before either takes the send lock.
    answered_task("race")
    # The two callers and this thread: passed once both callers made their first read.
    reads = threading.Barrier(3, timeout=10)
    real_get = plugin.store.get
    outer_read = threading.local()

    def get_then_wait(task_id):
        task = real_get(task_id)
        if task_id == "race" and not getattr(outer_read, "done", False):
            outer_read.done = True
            reads.wait()
        return task

    plugin.store.get = get_then_wait
    outcomes: list[str] = []

    def follow():
        try:
            plugin.follow_up("race", "second turn")
            outcomes.append("sent")
        except ValueError as exc:
            outcomes.append(f"refused: {exc}")

    callers = [threading.Thread(target=follow) for _ in range(2)]
    with plugin._send_lock:
        for caller in callers:
            caller.start()
        reads.wait()
        # Both outer reads done; let the callers queue on the send lock with the same snapshot.
        time.sleep(0.2)
    for caller in callers:
        caller.join(10)
    plugin.store.get = real_get
    check(sorted(o.split(":")[0] for o in outcomes) == ["refused", "sent"] and sent == ["race"],
          "two follow_ups that read the task at once start one turn; the other is refused", outcomes=outcomes,
          sends=sent)

    # 2. A tool follow_up after an answer and after an error: neither passes for the new turn's.
    for keyed in (False, True):
        task_id = f"clear-{keyed}"
        answered_task(task_id, error="earlier error", **({"key": "k"} if keyed else {}))
        plugin.follow_up(task_id, "next", keyed=keyed)
        task = plugin.store.get(task_id)
        check(task.get("last_answer") is None and task.get("error") is None and task.get("status") == "starting",
              f"a {'keyed' if keyed else 'tool'} follow_up clears the previous turn's answer and error",
              task={k: task.get(k) for k in ("status", "last_answer", "error")})

    # 3. The earlier turn's worker ends after a follow_up took the task.
    answered_task("late", status="running")
    old_turn = plugin.store.get("late")["turn_token"]
    plugin.store.update("late", status="answered", last_answer=None)
    plugin.follow_up("late", "next turn")
    new_turn = plugin.store.get("late")["turn_token"]
    plugin.store.update("late", status="running", admission_id="a2")
    before = len(ctx.injected)
    plugin._deliver("late", "Answer from analyst for task late: OLD", status="answered", last_answer="OLD",
                    turn=old_turn)
    task = plugin.store.get("late")
    check(new_turn != old_turn and task.get("status") == "running" and task.get("last_answer") is None
          and task.get("turn_token") == new_turn and not any("OLD" in t for t, _ in ctx.injected[before:])
          and [e["kind"] for e in task.get("outbox") or []] == ["earlier_result"] and "OLD" in task["outbox"][0]["text"],
          "a late answer of the earlier turn leaves the newer turn's state and is still queued as a notice, held "
          "while the newer turn runs",
          task={k: task.get(k) for k in ("status", "last_answer", "turn_token")}, new_turn=new_turn)
    plugin._deliver("late", "Answer from analyst for task late: NEW", status="answered", last_answer="NEW",
                    turn=new_turn)
    task = plugin.store.get("late")
    check(task.get("status") == "answered" and task.get("last_answer") == "NEW",
          "the newer turn's own answer is recorded", task={k: task.get(k) for k in ("status", "last_answer")})

    # The worker of the earlier turn loses its connection after the follow_up: the new turn's approval stays.
    answered_task("lost", status="running", turn_token="t-old")
    newer_approval = {"prompt_id": "p-new", "command": "rm -rf x", "raised_at": time.time()}

    async def cut_off(task_id, text, create, accepted, ready):
        ready.set()
        plugin.store.update(task_id, turn_token="t-new", status="running", pending_approval=newer_approval)
        raise ConnectionError("connection lost")

    real_send_and_wait = plugin._send_and_wait
    plugin._send_and_wait = cut_off
    plugin._run_send("lost", "x", False, {}, threading.Event())
    plugin._send_and_wait = real_send_and_wait
    task = plugin.store.get("lost")
    check(task.get("status") == "running" and task.get("pending_approval") == newer_approval
          and task.get("error") is None,
          "a cut-off wait of the earlier turn neither marks the task wait_lost nor drops the newer turn's approval",
          task={k: task.get(k) for k in ("status", "error", "pending_approval")})

    # 4. An approval that ended with its turn, before the command reported back.
    approval = {"prompt_id": "p-int", "command": "make deploy", "raised_at": time.time() - 5, "timeout_s": 60}
    answered_task("interrupted", status="running", settling_approval=approval)
    before = len(ctx.injected)
    plugin._approval_ended("interrupted", "p-int", ("interrupted", ""))
    told = " ".join(t for t, _ in ctx.injected[before:])
    check("whether it ran is unknown" in told and "did not run" not in told,
          "an approval interrupted before the command reported back says its outcome is unknown", notice=told)

    # 5. The manager answers an approval; the target refuses it, or the approval ends while it is forwarded.
    # This fakes _respond, so it covers the layer above it; check 13 covers _respond with the gateway's reply.
    import tools.approval as approval_mod

    def open_approval(task_id: str) -> dict:
        pending = {"prompt_id": f"p-{task_id}", "command": "rm -rf build", "description": "delete build",
                   "choices": ["once", "session", "always", "deny"], "execution_generation": 1,
                   "raised_at": time.time(), "expires_at": time.time() + 60, "timeout_s": 60}
        answered_task(task_id, status="running", pending_approval=pending)
        return pending

    def manager_says(choice: str):
        def request(tool, reason, rule_key=None):
            plugin.on_approval_response(pattern_key=f"plugin_rule:{rule_key}", choice=choice)
            return {"approved": choice != "deny"}
        return request

    real_request = approval_mod.request_tool_approval
    approval_mod.request_tool_approval = manager_says("once")
    try:
        for name, target, ends_in_flight in (("refused", False, False), ("refused-ended", False, True),
                                             ("taken-ended", True, True)):
            open_approval(name)

            async def respond(task, pending, choice, name=name, target=target, ends_in_flight=ends_in_flight):
                if ends_in_flight:
                    plugin._approval_ended(name, f"p-{name}", ("denied" if not target else "ran", ""))
                return {"ok": True, "result": {}} if target else {"ok": False, "error": "RuntimeError: refused"}

            plugin._respond = respond
            before = len(ctx.injected)
            answer = plugin.approval(name)
            if not ends_in_flight:
                plugin._approval_ended(name, f"p-{name}", ("denied", ""))
            task = plugin.store.get(name)
            endings = [t for t, _ in ctx.injected[before:] if "the approval for `rm -rf build`" in t]
            relayed = bool((task.get("last_approval") or {}).get("relayed"))
            if target:
                ok = answer["forwarded"] is True and not endings
                what = ("an answer the target took while its approval ended is forwarded, and the manager gets no "
                        "second outcome notice")
            else:
                ok = answer["forwarded"] is False and not relayed and len(endings) == 1
                what = (f"a forward the target refused{' while its approval ended' if ends_in_flight else ''} is "
                        f"not reported as forwarded, and the outcome notice reaches the manager once")
            check(ok, what, forwarded=answer["forwarded"], relayed=relayed, endings=endings)
    finally:
        approval_mod.request_tool_approval = real_request

    # 6. A call running beside the gated one completes while the approval is open.
    def frame(kind: str, **payload) -> dict:
        return {"type": kind, "session_id": "s-par", "payload": payload}

    blocked = json.dumps({"status": "blocked", "user_summary": "x"})
    for name, gated_result, expect in (("par-ran", "{}", "ran"), ("par-blocked", blocked, None)):
        answered_task(name, status="running", session_id="s-par")
        plugin._on_target_frame(name, frame("tool.start", tool_id="gated", name="terminal"))
        plugin._on_target_frame(name, frame("tool.start", tool_id="beside", name="write_file"))
        plugin._on_target_frame(name, frame("approval.request", prompt_id=f"p-{name}", command="rm -rf build",
                                            timeout_s=60))
        plugin._on_target_frame(name, frame("tool.complete", tool_id="beside", name="write_file", result="{}"))
        open_after_beside = bool(plugin.store.get(name).get("pending_approval"))
        if gated_result == "{}":
            plugin._on_target_frame(name, frame("approval.settled", prompt_id=f"p-{name}"))
        plugin._on_target_frame(name, frame("tool.complete", tool_id="gated", name="terminal", result=gated_result))
        task = plugin.store.get(name)
        last = task.get("last_approval") or {}
        ended = not task.get("pending_approval") and not task.get("settling_approval")
        ok = open_after_beside and ended and (last.get("outcome") == expect if expect else last.get("outcome") != "ran")
        check(ok, f"a call beside the gated one does not end its approval; the gated call's "
                  f"{'run' if expect else 'blocked result'} does",
              open_after_beside=open_after_beside, outcome=last.get("outcome"), calls=last.get("calls"))
    # A call that started after the approval was raised is not the gated call either.
    answered_task("par-late", status="running", session_id="s-par")
    plugin._on_target_frame("par-late", frame("tool.start", tool_id="gated", name="terminal"))
    plugin._on_target_frame("par-late", frame("approval.request", prompt_id="p-late", command="rm -rf x", timeout_s=60))
    plugin._on_target_frame("par-late", frame("tool.start", tool_id="later", name="read_file"))
    plugin._on_target_frame("par-late", frame("tool.complete", tool_id="later", name="read_file", result=blocked))
    check(bool(plugin.store.get("par-late").get("pending_approval")),
          "a call started after the approval was raised does not end it")

    # 7. A status read of an earlier turn ends while a follow_up starts a newer one.
    answered_task("refresh", status="wait_lost", last_answer=None, turn_token="t-read")

    async def read_then_follow_up(task):
        plugin.store.update("refresh", turn_token="t-newer", status="starting", last_answer=None)
        return {"status": "terminal", "outcome": "completed", "result": {"final_response": "STALE"}}

    plugin._read_receipt = read_then_follow_up
    out = plugin._refresh(plugin.store.get("refresh"))
    plugin._read_receipt = terminal_receipt
    task = plugin.store.get("refresh")
    check(task.get("status") == "starting" and task.get("last_answer") is None and "STALE" not in json.dumps(out),
          "a status read of the earlier turn does not overwrite the newer turn's state",
          task={k: task.get(k) for k in ("status", "last_answer", "turn_token")}, out=out)

    # 8. Frames queued before the terminal receipt reach the plugin.
    import asyncio

    seen: list[str] = []
    conn = mod._Connection(Path("/nonexistent"), on_frame=lambda f: seen.append(f["type"]))

    class FakeClient:
        def __init__(self):
            self.events = asyncio.Queue()

        async def rpc(self, method, **params):
            return {"status": "terminal"}

    async def queued_then_terminal():
        conn.client = FakeClient()
        for kind in ("approval.settled", "tool.complete"):
            conn.client.events.put_nowait({"method": "event", "params": {"type": kind, "payload": {}}})
        await conn.wait_terminal("s", "a", time.monotonic() + 5)

    asyncio.run(queued_then_terminal())
    check(seen == ["approval.settled", "tool.complete"],
          "frames queued before the terminal receipt are handed to the plugin", seen=seen)

    # 9. A protected-file write (SOUL.md) whose approval times out, is denied, withdrawn or approved. The result
    # is the one Hermes builds: the write gate's refusal, wrapped by tool_error, bounded for tool.complete.
    import tools.approval as approval_mod
    import tools.approval_gateway_wait as wait_mod
    from gateway.session_tool_events import bounded_result
    from tools.file_tools_write_guards import _request_protected_instruction_approval
    from tools.registry import tool_error

    real_cb, real_wait = approval_mod._gateway_notify_cb, wait_mod._await_gateway_decision
    approval_mod._gateway_notify_cb = lambda session_key: (lambda data: None)
    try:
        for decision, expect in (({"resolved": False}, "timeout"), ({"resolved": True, "choice": "deny"}, "denied"),
                                 ({"cancelled": "turn interrupted"}, "cancelled"),
                                 ({"notify_failed": True}, "notify_failed"),
                                 ({"resolved": True, "choice": "once"}, "ran")):
            wait_mod._await_gateway_decision = lambda *a, _d=decision, **k: _d
            refusal = _request_protected_instruction_approval(["SOUL.md"])
            raw = tool_error(refusal) if refusal else json.dumps({"success": True})
            name = f"soul-{expect}"
            answered_task(name, status="running", session_id="s-soul")
            plugin._on_target_frame(name, {"type": "tool.start", "session_id": "s-soul",
                                           "payload": {"tool_id": "w", "name": "patch"}})
            plugin._on_target_frame(name, {"type": "approval.request", "session_id": "s-soul", "payload": {
                "prompt_id": f"p-{name}", "command": "<write to SOUL.md>", "timeout_s": 500}})
            plugin._on_target_frame(name, {"type": "approval.settled", "session_id": "s-soul",
                                           "payload": {"prompt_id": f"p-{name}"}})
            before = len(ctx.injected)
            plugin._on_target_frame(name, {"type": "tool.complete", "session_id": "s-soul", "payload": {
                "tool_id": "w", "name": "patch", "is_error": bool(refusal),
                "result": bounded_result(raw, bool(refusal))}})
            last = plugin.store.get(name).get("last_approval") or {}
            told = " ".join(t for t, _ in ctx.injected[before:])
            ok = last.get("outcome") == expect and (("the command ran" in told) == (expect == "ran"))
            check(ok, f"a protected-file write whose approval ended {expect} is reported as {expect}",
                  outcome=last.get("outcome"), notice=told[:300])
    finally:
        approval_mod._gateway_notify_cb, wait_mod._await_gateway_decision = real_cb, real_wait

    # 10. The earlier turn's connection hands over its message.complete after a follow_up's turn raised an approval.
    newer = {"prompt_id": "p-newer", "command": "rm -rf y", "raised_at": time.time(), "timeout_s": 60,
             "expires_at": time.time() + 60}
    answered_task("bound", status="running", session_id="s-bound", turn_token="t-new", pending_approval=newer)
    try:
        plugin._on_target_frame("bound", {"type": "message.complete", "session_id": "s-bound", "payload": {}},
                                "t-old")
        error = None
    except TypeError as exc:
        error = str(exc)
    task = plugin.store.get("bound")
    check(error is None and task.get("pending_approval") == newer and not task.get("last_approval"),
          "a late frame of the earlier turn leaves the newer turn's approval open",
          error=error, pending=task.get("pending_approval"), last=task.get("last_approval"))

    # A follow_up reserves the next turn while a frame of the current turn is being handled: it waits for it.
    current = {"prompt_id": "p-cur", "command": "rm -rf z", "raised_at": time.time(), "timeout_s": 60,
               "expires_at": time.time() + 60}
    answered_task("atomic", session_id="s-atomic", turn_token="t-cur", pending_approval=current)
    real_ended = plugin._approval_ended
    seen_turns: list[str] = []
    racer = threading.Thread(target=lambda: plugin.follow_up("atomic", "next"))

    def ended_slowly(task_id, prompt_id, outcome):
        if task_id == "atomic":
            racer.start()
            time.sleep(0.3)
            seen_turns.append(plugin.store.get("atomic").get("turn_token"))
        real_ended(task_id, prompt_id, outcome)

    plugin._approval_ended = ended_slowly
    try:
        plugin._on_target_frame("atomic", {"type": "message.complete", "session_id": "s-atomic", "payload": {}},
                                "t-cur")
    except TypeError as exc:
        seen_turns.append(f"error: {exc}")
    finally:
        plugin._approval_ended = real_ended
    if racer.is_alive() or racer.ident:
        racer.join(10)
    check(seen_turns == ["t-cur"] and plugin.store.get("atomic").get("turn_token") != "t-cur",
          "a follow_up does not reserve the next turn while a frame of the current turn is handled",
          seen_turns=seen_turns, turn=plugin.store.get("atomic").get("turn_token"))

    # 11. A second approval card for the same request while the first is open.
    open_approval("twice")
    responded: list[str] = []
    second: list[str] = []

    async def take(task, pending, choice):
        responded.append(choice)
        return {"ok": True, "result": {}}

    def first_card(tool, reason, rule_key=None):
        try:
            plugin.approval("twice")
            second.append("opened")
        except ValueError as exc:
            second.append(f"refused: {exc}")
        plugin.on_approval_response(pattern_key=f"plugin_rule:{rule_key}", choice="once")
        return {"approved": True}

    def other_card(tool, reason, rule_key=None):
        plugin.on_approval_response(pattern_key=f"plugin_rule:{rule_key}", choice="deny")
        return {"approved": False}

    cards = iter([first_card, other_card])
    plugin._respond = take
    approval_mod.request_tool_approval = lambda *a, **k: next(cards)(*a, **k)
    try:
        answer = plugin.approval("twice")
    finally:
        approval_mod.request_tool_approval = real_request
    check([s.split(":")[0] for s in second] == ["refused"] and responded == ["once"] and answer.get("choice") == "once",
          "a second card for an approval already open is refused; the first card's answer is the one forwarded",
          second=second, responded=responded, choice=answer.get("choice"))

    # 12. A task-specific status read of an answered task whose notices were refused.
    answered_task("refused-notices", outbox=[mod._notice("approval for x", "approval"),
                                             mod._notice("approval ended", "approval_end"),
                                             mod._notice("Answer: first answer", "result")])
    row = plugin.status("refused-notices")["tasks"][0]
    left = [e["kind"] for e in plugin.store.get("refused-notices").get("outbox") or []]
    check(row.get("notices_not_delivered") == ["approval for x", "approval ended"] and row.get("answer")
          and left == ["approval", "approval_end"],
          "status with the answer still lists the approval notices not delivered, without the answer's",
          notices=row.get("notices_not_delivered"), outbox=left)

    # 13. The manager answers an approval the target session had already ended: the real _respond reads the
    # gateway's {"status": "already_resolved"} reply, which does not raise.
    class AlreadyResolved:
        def __init__(self, home, on_frame=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def rpc(self, method, **params):
            return {"status": "already_resolved", "prompt_id": params.get("prompt_id")} if method == \
                "approval.respond" else {}

    del plugin._respond
    real_connection = mod._Connection
    mod._Connection = AlreadyResolved
    approval_mod.request_tool_approval = manager_says("once")
    try:
        open_approval("gone")
        before = len(ctx.injected)
        answer = plugin.approval("gone")
        plugin._approval_ended("gone", "p-gone", ("timeout", ""))
    finally:
        mod._Connection = real_connection
        approval_mod.request_tool_approval = real_request
    last = plugin.store.get("gone").get("last_approval") or {}
    endings = [t for t, _ in ctx.injected[before:] if "the approval for `rm -rf build`" in t]
    check(answer["forwarded"] is False and not last.get("relayed") and len(endings) == 1 and "expired" in endings[0],
          "an answer to an approval the target had already ended is not reported as forwarded, and its real "
          "outcome reaches the manager once", forwarded=answer["forwarded"], relayed=last.get("relayed"),
          endings=endings, note=answer.get("note"))

    # 14. Two approvals open at once in one session: each keeps its own card and ends on its own call.
    def two(kind: str, **payload) -> dict:
        return {"type": kind, "session_id": "s-two", "payload": payload}

    answered_task("two", status="running", session_id="s-two")
    plugin._on_target_frame("two", two("tool.start", tool_id="c1", name="terminal"))
    plugin._on_target_frame("two", two("approval.request", prompt_id="p1", command="rm -rf a", timeout_s=60))
    plugin._on_target_frame("two", two("tool.start", tool_id="c2", name="terminal"))
    plugin._on_target_frame("two", two("approval.request", prompt_id="p2", command="rm -rf b", timeout_s=60))
    both = sorted((plugin.store.get("two").get("approvals") or {}).keys())
    first_shown = (plugin.store.get("two").get("pending_approval") or {}).get("prompt_id")
    before = len(ctx.injected)
    plugin._on_target_frame("two", two("approval.settled", prompt_id="p1"))
    plugin._on_target_frame("two", two("tool.complete", tool_id="c1", name="terminal", result=blocked))
    after_first = sorted((plugin.store.get("two").get("approvals") or {}).keys())
    plugin._on_target_frame("two", two("approval.settled", prompt_id="p2"))
    plugin._on_target_frame("two", two("tool.complete", tool_id="c2", name="terminal", result="{}"))
    told = [t for t, _ in ctx.injected[before:] if "the approval for" in t]
    check(both == ["p1", "p2"] and first_shown == "p1" and after_first == ["p2"]
          and not plugin.store.get("two").get("approvals") and len(told) == 2
          and "`rm -rf a`" in told[0] and "the command ran" not in told[0]
          and "whether it ran is unknown" not in told[0] and "`rm -rf b`" in told[1]
          and "the command ran" in told[1],
          "two approvals open in one session are both kept, and each ends on its own call with its own outcome",
          both=both, first_shown=first_shown, after_first=after_first, told=told)

    # 15. The worker fails after _send stopped waiting for the acknowledgement (120 s): the failure is recorded.
    answered_task("ack", status="starting", turn_token="t-ack", waiter=mod._process_token(mod.os.getpid()),
                  admission_id=None, last_answer=None)

    async def fails_late(task_id, text, create, accepted, ready):
        raise ConnectionError("gateway went away")

    plugin._send_and_wait = fails_late
    plugin._run_send("ack", "x", False, {}, threading.Event())
    plugin._send_and_wait = real_send_and_wait
    task = plugin.store.get("ack")
    check(task.get("status") == "failed" and "gateway went away" in (task.get("error") or "") and not task.get("waiter"),
          "a worker failing after the acknowledgement wait gave up records the task failed, so it is not left "
          "'starting' for the life of the gateway", task={k: task.get(k) for k in ("status", "error", "waiter")})

    # 16. A wait cut off mid-turn with a tool call still open: the next turn's approval is not gated by that call.
    answered_task("stale", status="running", session_id="s-stale", turn_token="t-cut")
    plugin._on_target_frame("stale", {"type": "tool.start", "session_id": "s-stale",
                                      "payload": {"tool_id": "old", "name": "terminal"}})

    async def cut_mid_turn(task_id, text, create, accepted, ready):
        ready.set()
        raise ConnectionError("connection lost")

    plugin._send_and_wait = cut_mid_turn
    plugin._run_send("stale", "x", False, {}, threading.Event())
    plugin._send_and_wait = real_send_and_wait
    left_open = sorted(plugin._open_calls.get("stale", ()))
    plugin.store.update("stale", status="running", turn_token="t-next")
    plugin._on_target_frame("stale", {"type": "tool.start", "session_id": "s-stale",
                                      "payload": {"tool_id": "new", "name": "terminal"}})
    plugin._on_target_frame("stale", {"type": "approval.request", "session_id": "s-stale",
                                      "payload": {"prompt_id": "p-stale", "command": "rm -rf c", "timeout_s": 60}})
    calls = (plugin.store.get("stale").get("pending_approval") or {}).get("calls")
    plugin._on_target_frame("stale", {"type": "tool.complete", "session_id": "s-stale",
                                      "payload": {"tool_id": "new", "name": "terminal", "result": "{}"}})
    last = plugin.store.get("stale").get("last_approval") or {}
    check(left_open == [] and calls == ["new"] and last.get("prompt_id") == "p-stale" and last.get("outcome") == "ran",
          "a turn cut off with a call open leaves no stale call to keep the next turn's approval from ending",
          left_open=left_open, calls=calls, outcome=last.get("outcome"))

    # 17. A task answered after its approval watch was lost is not reported cut off by a restart at the next load.
    answered_task("done", status="running", turn_token="t-done", waiter="999999999:0",
                  last_approval={"prompt_id": "p-done", "command": "x", "outcome": "watch_lost",
                                 "raised_at": time.time() - 10, "settled_at": time.time() - 5})
    plugin._deliver("done", "Answer from analyst for task done: D", status="answered", last_answer="D",
                    turn="t-done")
    waiter = plugin.store.get("done").get("waiter")
    found = plugin.recover_after_restart()
    check(waiter is None and "done" not in found,
          "an answered task keeps no waiter, so a later load does not report it cut off by a restart",
          waiter=waiter, found=found)

    # 18. No receipt is read while the key or send lock is held: one unreachable target must not stall the others.
    held: list[tuple[str, bool]] = []
    real_read_turn = plugin._read_turn

    def read_turn(task):
        held.append((task["task_id"], plugin._key_lock.locked() or plugin._send_lock.locked()))
        return real_read_turn(task)

    plugin._read_turn = read_turn
    answered_task("unlocked")
    plugin.follow_up("unlocked", "next")
    plugin.store.update("k-start", task_id="k-start", profile="analyst", key="k-unlocked", caller_session_key=None,
                        title="Task k-start", created_at=time.time(), status="starting", session_id=None, turns=[],
                        waiter="999999999:0", turn_token="t-k")
    try:
        mod.ProfileTasks.send_keyed(plugin, "k-unlocked", "analyst", "x")
    except ValueError:
        pass  # No analyst target in this isolated home; the read before the lock is what is checked.
    plugin._read_turn = real_read_turn
    check([t for t, _ in held] == ["unlocked", "k-start"] and not any(locked for _, locked in held),
          "follow_up and a keyed send read the target turn before taking their lock", held=held)

    # 19. Two gated calls start before either approval is raised: both approvals list both calls.
    def dual(kind: str, **payload) -> dict:
        return {"type": kind, "session_id": "s-dual", "payload": payload}

    answered_task("dual", status="running", session_id="s-dual")
    for tool_id in ("c1", "c2"):
        plugin._on_target_frame("dual", dual("tool.start", tool_id=tool_id, name="patch"))
    plugin._on_target_frame("dual", dual("approval.request", prompt_id="pa", command="patch A", timeout_s=60))
    plugin._on_target_frame("dual", dual("approval.request", prompt_id="pb", command="patch B", timeout_s=60))
    before = len(ctx.injected)
    plugin._on_target_frame("dual", dual("approval.settled", prompt_id="pa"))
    plugin._on_target_frame("dual", dual("tool.complete", tool_id="c1", name="patch", result="{}"))
    plugin._on_target_frame("dual", dual("approval.settled", prompt_id="pb"))
    plugin._on_target_frame("dual", dual("tool.complete", tool_id="c2", name="patch", result=blocked))
    told = [t for t, _ in ctx.injected[before:] if "the approval for" in t]
    plugin._on_target_frame("dual", dual("tool.start", tool_id="c3", name="read_file"))
    plugin._on_target_frame("dual", dual("tool.complete", tool_id="c3", name="read_file", result="{}"))
    after_read = [t for t, _ in ctx.injected[before:] if "the approval for" in t]
    still_open = sorted((plugin.store.get("dual").get("approvals") or {}).keys())
    definite = [t for t in after_read if "the command ran" in t or "did not run" in t]
    check(not definite and len(told) == 1 and "whether it ran is unknown" in told[0] and after_read == told
          and len(still_open) == 1,
          "two approvals raised over the same two calls are never told a definite outcome, and an unrelated "
          "later result ends neither", told=after_read, still_open=still_open)
    plugin._on_target_frame("dual", dual("message.complete"))
    told = [t for t, _ in ctx.injected[before:] if "the approval for" in t]
    check(len(told) == 2 and "`patch B`" in told[1] and "whether it ran is unknown" in told[1],
          "the approval left open by that pair ends with the turn, with no definite outcome", told=told)

    # 20. The other order: c1 is blocked first, then c2 runs. The approval left after c1 may own either call.
    answered_task("dual2", status="running", session_id="s-dual2")

    def dual2(kind: str, **payload) -> dict:
        return {"type": kind, "session_id": "s-dual2", "payload": payload}

    for tool_id in ("c1", "c2"):
        plugin._on_target_frame("dual2", dual2("tool.start", tool_id=tool_id, name="patch"))
    plugin._on_target_frame("dual2", dual2("approval.request", prompt_id="pa", command="patch A", timeout_s=60))
    plugin._on_target_frame("dual2", dual2("approval.request", prompt_id="pb", command="patch B", timeout_s=60))
    before = len(ctx.injected)
    plugin._on_target_frame("dual2", dual2("approval.settled", prompt_id="pa"))
    plugin._on_target_frame("dual2", dual2("tool.complete", tool_id="c1", name="patch", result=blocked))
    plugin._on_target_frame("dual2", dual2("approval.settled", prompt_id="pb"))
    plugin._on_target_frame("dual2", dual2("tool.complete", tool_id="c2", name="patch", result="{}"))
    told = [t for t, _ in ctx.injected[before:] if "the approval for" in t]
    definite = [t for t in told if "the command ran" in t or "did not run" in t]
    check(not definite and len(told) == 2 and all("whether it ran is unknown" in t for t in told)
          and not plugin.store.get("dual2").get("approvals"),
          "an approval that shared a call with another keeps no definite outcome after the other one ended",
          told=told)

    # 21. A follow_up reserves its turn and the gateway restarts before the target accepts it.
    answered_task("fu-restart", last_answer=None)
    plugin.follow_up("fu-restart", "next")
    reserved = plugin.store.get("fu-restart").get("admission_id")
    plugin.store.update("fu-restart", waiter="999999999:0")

    async def previous_turn_receipt(task):
        return {"status": "terminal", "result": {"final_response": "PREVIOUS TURN"}}

    plugin._read_receipt = previous_turn_receipt
    found = plugin.recover_after_restart()
    out = plugin.status("fu-restart")
    plugin._read_receipt = terminal_receipt
    task = plugin.store.get("fu-restart")
    check(reserved is None and "fu-restart" in found and task.get("status") == "failed"
          and "PREVIOUS TURN" not in json.dumps(out) and task.get("last_answer") is None,
          "a follow_up cut off by a restart before the target accepted it is not given the previous turn's answer",
          reserved=reserved, status=task.get("status"), out=out)

    print(f"{failures} failure(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
