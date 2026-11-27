"""A task is followed to its real end, with a fake gateway behind the real connection class:

- a turn started by an injection in a followed session reaches the manager, with its place in ``turns``;
- an open sub-task of the target profile, started from that session, makes the turn end an update; one from
  another session does not count; a delivered answer no longer counts;
- an update is not sent to the manager (status shows it), unless its turn failed;
- ``WAITING:`` counts on the last line only; a queued prompt and a background command count; with that internal
  missing the load says so and the other two conditions still apply;
- a failed turn is an update while something is open, else the task failed;
- follow_up and an approval answer go through the observer's connection (no session.resume); follow_up is refused
  during a turn;
- the follow limit closes the observer; status then returns a later turn;
- a restart gives a paused task its observer back; a refused notice is retried without a tool call;
- result(key) shows waiting_subwork with its update, then the answer.

Run: ``PT_SCRIPT=tests/test_follow_real_end.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path

PLUGIN = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "__init__.py")


def load():
    spec = importlib.util.spec_from_file_location("profile_tasks_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    profile_name = "default"

    def __init__(self):
        from hermes_cli.plugins_state import PluginState
        self.state = PluginState("profile-tasks")
        self.injected: list[str] = []
        self.accept = True
        self.config: dict = {}
        self._manager = types.SimpleNamespace(has_gateway_message_injector=True)
        self.tools: list = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def inject_message(self, text, session_key=None):
        if not self.accept:
            return False
        self.injected.append(text)
        return True

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_hook(self, name, callback):
        pass

    def on_unload(self, callback):
        pass


class Gateway:
    """The target profile's gateway: records calls, ends turns on request, tells how many connections are open."""

    def __init__(self):
        self.calls: list[tuple[int, str, dict]] = []
        self.done: dict[str, str] = {}
        self.failed: set[str] = set()
        self.queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.open = 0
        self.opened = 0
        self.count = 0
        self.running = False
        self.messages: list[dict] = []
        self.lock = threading.Lock()

    def emit(self, frame_type: str, **payload) -> None:
        event = {"params": {"type": frame_type, "session_id": "s1", "payload": payload}}
        for loop, queue in list(self.queues):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def end_turn(self, admission: str, text: str, status: str = "complete", error: str | None = None) -> None:
        """The turn ends: the receipt turns terminal, then the end frame goes to every connection."""
        self.done[admission] = text if status == "complete" else ""
        if status != "complete":
            self.failed.add(admission)
        self.running = False
        self.emit("message.complete", text=text, status=status, admission_id=admission,
                  **({"error": error} if error else {}))

    def start_injected(self, admission: str) -> None:
        self.running = True
        self.emit("message.start", admission_id=admission)

    def methods(self) -> list[str]:
        return [m for _, m, _ in self.calls]

    def methods_of(self, conn: int) -> list[str]:
        return [m for c, m, _ in self.calls if c == conn]


def main() -> int:
    mod = load()
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:900], flush=True)

    def wait_for(condition, seconds: float = 10.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.05)
        return bool(condition())

    gw = Gateway()
    real_connection = mod._Connection

    class FakeClient:
        def __init__(self, conn_id: int):
            self.conn_id, self.events = conn_id, asyncio.Queue()

        async def rpc(self, method: str, **params):
            gw.calls.append((self.conn_id, method, params))
            if method == "session.create":
                return {"session_id": "s1", "stored_session_id": "k1"}
            if method == "session.resume":
                return {"session_id": "s1", "stored_session_id": "k1", "messages": list(gw.messages)}
            if method == "prompt.submit":
                with gw.lock:
                    gw.count += 1
                    gw.running = True
                    return {"admission_id": f"adm-{gw.count}"}
            if method == "prompt.receipt":
                adm = params["admission_id"]
                if adm in gw.done:
                    outcome = "interrupted" if adm in gw.failed else "completed"
                    return {"status": "terminal", "outcome": outcome, "result": {"final_response": gw.done[adm]}}
                return {"status": "started", "execution_generation": 1}
            if method == "session.info":
                return {"running": gw.running, "mcp_servers": []}
            if method == "approval.respond":
                return {"status": "resolved"}
            return {}

    class FakeConnection(real_connection):
        async def __aenter__(self):
            gw.open += 1
            gw.opened += 1
            self.conn_id = gw.opened
            self.client = FakeClient(self.conn_id)
            gw.queues.append((asyncio.get_running_loop(), self.client.events))
            return self

        async def __aexit__(self, *exc):
            gw.open -= 1
            gw.queues[:] = [q for q in gw.queues if q[1] is not self.client.events]

    mod._Connection = FakeConnection

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])
    # Each check reads one task's end while earlier tasks stay open; the grouping is in test_batch_answers.py.
    plugin.batch_seconds = 0
    mod._PLUGIN = plugin

    # The target profile's plugin state, read by the plugin: written here as the target's own plugin would.
    from hermes_cli.plugins_state import PluginState
    target_home = plugin._home("implementer")
    folder = target_home / "plugin-data" / PluginState("profile-tasks").data_dir.name
    folder.mkdir(parents=True, exist_ok=True)

    def set_subtasks(tasks: dict) -> None:
        (folder / "state.json").write_text(json.dumps({mod.STATE_KEY: tasks}))

    def sub(status: str, caller: str = "s1", delivered: bool | None = None, profile: str = "reviewer") -> dict:
        return {"status": status, "caller_session_id": caller, "profile": profile,
                **({} if delivered is None else {"delivered": delivered})}

    def live_sessions(**session) -> None:
        served = types.ModuleType("tui_gateway.server")
        served._sessions = {"s1": {"session_key": "k1", **session}}
        served._sessions_lock = threading.RLock()
        served._broadcast_global_event = lambda *args, **kwargs: None
        sys.modules["tui_gateway.server"] = served

    from tui_gateway import session_notifications
    background = {"owns": False}
    session_notifications._session_owns_pending_process_notification = lambda session: background["owns"]
    live_sessions()
    set_subtasks({})

    def new_task() -> str:
        before = gw.count
        task_id = plugin._new_task("implementer", "work", "mgr")
        sent = plugin._send(task_id, "do it", create=True)
        assert sent.get("accepted"), sent
        return task_id, f"adm-{before + 1}"

    def last_text() -> str:
        return ctx.injected[-1] if ctx.injected else ""

    def paused_quietly(task_id: str, n: int, reasons: list, text: str) -> bool:
        """The task paused with *reasons*, status shows the update, and nothing new reached the manager."""
        if not wait_for(lambda: plugin.store.get(task_id).get("last_update") == text):
            return False
        time.sleep(0.5)
        row = plugin.status(task_id)["tasks"][0]
        return (len(ctx.injected) == n and not plugin.store.get(task_id).get("outbox")
                and row["status"] == "waiting_subwork" and row.get("update") == text
                and row.get("waiting_on") == reasons)

    def close_task(task_id: str) -> None:
        # Ends the observer of a task a scenario leaves open.
        observer = plugin._live_observer(task_id)
        if observer is not None:
            observer.stop()
            wait_for(lambda: plugin._live_observer(task_id) is None)

    # 1. A turn started by an injection reaches the manager, with its place in turns.
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "first step done\nWAITING: CI")
    check(paused_quietly(task_id, n, ["CI"], "first step done\nWAITING: CI"),
          "the submitted turn that declares WAITING: is an update status shows, not sent to the manager",
          injected=ctx.injected[n:], row=plugin.status(task_id)["tasks"][0])
    check(plugin.store.get(task_id)["status"] == "waiting_subwork", "the task is waiting_subwork",
          status=plugin.store.get(task_id)["status"])
    n = len(ctx.injected)
    gw.start_injected("inj-1")
    gw.end_turn("inj-1", "CI is green, all done")
    wait_for(lambda: len(ctx.injected) > n)
    task = plugin.store.get(task_id)
    check(last_text().startswith(f"Answer from implementer for task {task_id} (session s1), finished:")
          and "CI is green" in last_text() and task["status"] == "answered",
          "a turn started by an injection reaches the manager as the final answer", injected=ctx.injected[n:])
    check(task["turns"] == [adm, "inj-1"], "the injected turn takes its place in turns", turns=task["turns"])
    check(wait_for(lambda: gw.open == 0 and plugin._live_observer(task_id) is None),
          "the observer closes at the real end", open=gw.open)
    check("session.resume" not in gw.methods(), "no session.resume was made for a task that never left its session",
          methods=gw.methods())

    # 1b. A turn without an admission id is named observed-<n>.
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: a card")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    gw.emit("message.complete", text="more\nWAITING: a card", status="complete")
    wait_for(lambda: len(plugin.store.get(task_id)["turns"]) == 2)
    check(plugin.store.get(task_id)["turns"] == [adm, "observed-1"], "a turn with no admission id is observed-<n>",
          turns=plugin.store.get(task_id)["turns"])
    close_task(task_id)

    # 2. An open sub-task started from the session: update naming it.
    set_subtasks({"t-sub1": sub("running")})
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "review requested")
    check(paused_quietly(task_id, n, ["task t-sub1 (reviewer)"], "review requested"),
          "an open sub-task makes the turn end an update naming it, not sent to the manager",
          injected=ctx.injected[n:])

    # 3. An answered sub-task whose answer is not delivered still counts; delivered, the turn is final.
    n = len(ctx.injected)
    set_subtasks({"t-sub1": sub("answered", delivered=False)})
    gw.start_injected("inj-2")
    gw.end_turn("inj-2", "verdict read, fixing")
    check(paused_quietly(task_id, n, ["task t-sub1 (reviewer)"], "verdict read, fixing"),
          "an answered sub-task with an undelivered answer is still an update", injected=ctx.injected[n:])
    n = len(ctx.injected)
    set_subtasks({"t-sub1": sub("answered", delivered=True)})
    gw.start_injected("inj-3")
    gw.end_turn("inj-3", "fixed and pushed")
    wait_for(lambda: len(ctx.injected) > n)
    check(last_text().startswith("Answer from implementer") and plugin.store.get(task_id)["status"] == "answered",
          "once delivered, the next turn without other conditions is the answer", injected=ctx.injected[n:])
    check(wait_for(lambda: plugin._live_observer(task_id) is None and gw.open == 0), "the connection is closed")

    # 4. A sub-task started from another session of the same profile does not count.
    set_subtasks({"t-other": sub("running", caller="s-other")})
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "done")
    wait_for(lambda: len(ctx.injected) > n)
    check(last_text().startswith("Answer from implementer"), "a sub-task of another session does not count",
          injected=ctx.injected[n:])
    set_subtasks({})

    # 5. WAITING: only counts on the last non-empty line.
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "WAITING: CI\nbut then I finished it all\n\n")
    wait_for(lambda: len(ctx.injected) > n)
    check(last_text().startswith("Answer from implementer"), "WAITING: in the middle of the text does not count",
          injected=ctx.injected[n:])

    # 6. A queued prompt in the live session.
    live_sessions(queued_prompt="next")
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "done for now")
    check(paused_quietly(task_id, n, ["a queued turn"], "done for now"), "a queued prompt makes the turn end an update",
          injected=ctx.injected[n:])
    close_task(task_id)
    live_sessions()

    # 7. A background command that will notify.
    background["owns"] = True
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "started the build")
    check(paused_quietly(task_id, n, ["a background command"], "started the build"),
          "a background command makes the turn end an update", injected=ctx.injected[n:])
    close_task(task_id)
    # With the internal missing: reported at load, only conditions 1 and 4 apply.
    plugin.soft_missing = ["tui_gateway.session_notifications._session_owns_pending_process_notification"]
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "started the build")
    wait_for(lambda: len(ctx.injected) > n)
    check(last_text().startswith("Answer from implementer"), "without the internal, the background command is ignored",
          injected=ctx.injected[n:])
    plugin.soft_missing = []
    background["owns"] = False
    real_check = mod.check_internals
    mod.check_internals = lambda ctx_=None: ["tui_gateway.session_notifications._session_owns_pending_process_notification"]
    try:
        load_ctx = FakeCtx()
        mod.register(load_ctx)
        registered = mod._PLUGIN
        check(registered.soft_missing and not registered.missing and load_ctx.tools,
              "register reports the missing internal and keeps the tool", soft=registered.soft_missing,
              missing=registered.missing)
    finally:
        mod.check_internals = real_check
        mod._PLUGIN = plugin
    check("tui_gateway.session_notifications" in " ".join(real_check()) or real_check() == [],
          "check_internals checks the session_notifications internal")

    # 8. A failed turn: an update while something is open, else failed.
    set_subtasks({"t-sub2": sub("running")})
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "", status="error", error="model crashed")
    wait_for(lambda: len(ctx.injected) > n)
    check("this turn failed: model crashed" in last_text() and "Not finished: it is waiting on task t-sub2" in last_text()
          and len(ctx.injected) == n + 1 and plugin.store.get(task_id)["status"] == "waiting_subwork",
          "a failed turn with an open sub-task is an update sent to the manager and the task stays open",
          injected=ctx.injected[n:])
    close_task(task_id)
    set_subtasks({})
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "", status="error", error="model crashed")
    wait_for(lambda: len(ctx.injected) > n)
    check("did not complete: model crashed" in last_text() and plugin.store.get(task_id)["status"] == "failed",
          "a failed turn without a sub-task fails the task", injected=ctx.injected[n:])

    # 8b. A stopping gateway: an interrupted turn and a cut connection are left for recover_after_restart.
    stopping = {"on": True}
    plugin._gateway_stopping = lambda: stopping["on"]
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "Operation interrupted", status="interrupted")
    time.sleep(1.0)
    task = plugin.store.get(task_id)
    check(len(ctx.injected) == n and task["status"] in mod.WAITING and task.get("waiter") and not task.get("outbox"),
          "an interrupted turn while the gateway stops sends nothing and leaves the task as it was",
          status=task["status"], injected=ctx.injected[n:])
    plugin._wait_cut(task_id, task.get("turn_token"), "Gateway disconnected")
    task = plugin.store.get(task_id)
    check(len(ctx.injected) == n and task["status"] in mod.WAITING and not task.get("outbox"),
          "a cut connection while the gateway stops sends nothing", status=task["status"])
    close_task(task_id)
    stopping["on"] = False
    task_id, adm = new_task()
    n = len(ctx.injected)
    gw.end_turn(adm, "Operation interrupted", status="interrupted")
    wait_for(lambda: len(ctx.injected) > n)
    check("did not complete: Operation interrupted" in last_text() or "did not complete: interrupted" in last_text(),
          "an interrupt of a live gateway is still reported", injected=ctx.injected[n:])
    del plugin._gateway_stopping

    # 9. follow_up in waiting_subwork goes through the observer, with no session.resume; refused during a turn.
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: reviewer")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    opened, calls = gw.opened, len(gw.calls)
    from gateway import session_context
    session_context.bound_session_key = lambda: "mgr"
    sent = plugin.follow_up(task_id, "the reviewer says go on")
    new_calls = [m for _, m, _ in gw.calls[calls:]]
    check(sent.get("accepted") and "session.resume" not in new_calls and "prompt.submit" in new_calls
          and gw.opened == opened,
          "follow_up in waiting_subwork is accepted on the observer's connection, with no session.resume",
          sent=sent, calls=new_calls, new_connections=gw.opened - opened)
    refused = ""
    try:
        plugin.follow_up(task_id, "again")
    except ValueError as exc:
        refused = str(exc)
    check("has a turn running" in refused, "follow_up during a turn is refused", refused=refused)

    # 10. An approval answer goes through the observer's connection and the next turn still reaches the manager.
    plugin.store.update(task_id, pending_approval={"prompt_id": "p1", "execution_generation": 1, "command": "x"})
    calls = len(gw.calls)
    result = asyncio.run(plugin._respond(plugin.store.get(task_id), {"prompt_id": "p1", "execution_generation": 1},
                                         "once"))
    new_calls = [m for _, m, _ in gw.calls[calls:]]
    check(result.get("ok") and new_calls == ["approval.respond"],
          "an approval answer goes through the observer's connection, with no session.resume", result=result,
          calls=new_calls)
    n = len(ctx.injected)
    gw.end_turn(f"adm-{gw.count}", "approved and finished")
    # The approval's end notice and the answer are two injections, in that order, a moment apart.
    wait_for(lambda: any(text.startswith("Answer from implementer") for text in ctx.injected[n:]))
    check(last_text().startswith("Answer from implementer"), "the turn after the approval still reaches the manager",
          injected=ctx.injected[n:])

    # 11. The follow limit.
    ctx.config["follow_timeout_seconds"] = 2
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: nobody")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    check(wait_for(lambda: plugin.store.get(task_id)["status"] == "follow_lost", 15),
          "the follow limit ends the following", status=plugin.store.get(task_id)["status"])
    wait_for(lambda: "no news for" in last_text())
    check("(implementer): no news for 2 s" in last_text() and "waiting on nobody" in last_text()
          and "the session still exists" in last_text(), "the follow_limit notice says what it waited on",
          injected=ctx.injected[-1:])
    check(wait_for(lambda: plugin._live_observer(task_id) is None and gw.open == 0), "the connection is closed")
    gw.messages = [{"role": "user", "text": "x"}, {"role": "assistant", "text": "late verdict: fine"}]
    gw.running = False
    row = plugin.status(task_id)["tasks"][0]
    check(row.get("answer") == "late verdict: fine" and row["status"] == "answered",
          "status returns a turn that arrived after the follow limit", row=row)
    ctx.config.pop("follow_timeout_seconds")

    # 12. A restart: the paused task gets its observer back.
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: CI")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    close_task(task_id)
    plugin.store.update(task_id, waiter="999999999:0", pause_reasons=["CI"], status="waiting_subwork")
    n, calls = len(ctx.injected), len(gw.calls)
    plugin.recover_after_restart()
    check(wait_for(lambda: plugin._live_observer(task_id) is not None), "the paused task has an observer again")
    wait_for(lambda: len(ctx.injected) > n)
    task = plugin.store.get(task_id)
    check("it is followed again" in last_text() and "waiting on CI" in last_text() and task["status"] == "waiting_subwork"
          and "session.resume" in [m for _, m, _ in gw.calls[calls:]],
          "a restart gives a followed-again notice, resumes the session and no wait_lost", task=task["status"],
          injected=ctx.injected[n:])
    n = len(ctx.injected)
    gw.start_injected("inj-r")
    gw.end_turn("inj-r", "CI green, finished")
    wait_for(lambda: len(ctx.injected) > n)
    check(last_text().startswith("Answer from implementer"), "the followed-again task reports its real end",
          injected=ctx.injected[n:])

    # 12b. A restart of a paused task with an approval open: followed again, the approval ended by the restart.
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: CI")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    close_task(task_id)
    approval = {"prompt_id": "p9", "command": "rm x", "stage": "pending", "raised_at": time.time()}
    plugin.store.update(task_id, waiter="999999999:0", pause_reasons=["CI"], status="waiting_subwork",
                        **mod._approval_fields({"p9": approval}))
    n = len(ctx.injected)
    closed = plugin.recover_after_restart()
    check(task_id not in closed and wait_for(lambda: plugin._live_observer(task_id) is not None),
          "a paused task with an approval open has an observer again after a restart", closed=closed)
    wait_for(lambda: len(ctx.injected) > n)
    task = plugin.store.get(task_id)
    check("it is followed again" in last_text() and "The approval for `rm x` ended with the restart" in last_text()
          and "after the wait limit" not in last_text() and task["status"] == "waiting_subwork"
          and not task.get("pending_approval") and not task.get("approvals")
          and (task.get("last_approval") or {}).get("outcome") == "interrupted",
          "the restart notice says the task is followed again and the approval ended", task=task["status"],
          last_approval=task.get("last_approval"), injected=ctx.injected[n:])
    n = len(ctx.injected)
    gw.start_injected("inj-ra")
    gw.end_turn("inj-ra", "approval redone, finished")
    wait_for(lambda: len(ctx.injected) > n)
    check(last_text().startswith("Answer from implementer") and "approval redone" in last_text(),
          "the followed-again task with the ended approval reports its real end", injected=ctx.injected[n:])

    # 13. A refused notice is tried again by the 60 s retry, with no tool call.
    task_id, adm = new_task()
    ctx.accept = False
    gw.end_turn(adm, "all done")
    wait_for(lambda: plugin.store.get(task_id).get("outbox"))
    n = len(ctx.injected)
    plugin.retry_seconds = 0.3
    retry = threading.Thread(target=plugin._retry_loop, daemon=True)
    retry.start()
    time.sleep(0.7)
    check(len(ctx.injected) == n and plugin.store.get(task_id).get("outbox"), "the notice stays queued while refused")
    ctx.accept = True
    check(wait_for(lambda: len(ctx.injected) > n and not plugin.store.get(task_id).get("outbox")),
          "the retry delivers the notice once the session accepts it", injected=ctx.injected[n:])
    check(mod.OUTBOX_RETRY_SECONDS == 60, "the retry interval is 60 s")
    plugin.retry_seconds = 3600
    close_task(task_id)

    # 14. Keyed: result(key) shows waiting_subwork with its update, then the answer at the end.
    set_subtasks({})
    key_task = plugin._new_task("implementer", "k", None, "key-1")
    sent = plugin._send(key_task, "do it", create=True)
    adm = f"adm-{gw.count}"
    gw.end_turn(adm, "half done\nWAITING: CI")
    wait_for(lambda: plugin.store.get(key_task)["status"] == "waiting_subwork")
    row = mod.result("key-1")
    check(row["status"] == "waiting_subwork" and "half done" in row.get("update", "") and "answer" not in row,
          "result(key) shows waiting_subwork with the last turn text in update", row=row)
    gw.start_injected("inj-k")
    gw.end_turn("inj-k", "all done")
    wait_for(lambda: plugin.store.get(key_task)["status"] == "answered")
    row = mod.result("key-1")
    check(row["status"] == "answered" and row.get("answer") == "all done", "result(key) gives the answer at the end",
          row=row)

    # 15. Past the wait limit, an answer status already handed over is not injected again at the turn's end.
    ctx.config["wait_timeout_seconds"] = 1
    task_id, adm = new_task()
    check(wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_timeout", 15),
          "the wait limit passes", status=plugin.store.get(task_id)["status"])
    gw.done[adm] = "the real answer"
    gw.running = False
    row = plugin.status(task_id)["tasks"][0]
    n = len(ctx.injected)
    gw.emit("message.complete", text="the real answer", status="complete", admission_id=adm)
    time.sleep(1.0)
    check(row.get("answer") == "the real answer" and row["status"] == "answered"
          and not any("the real answer" in t for t in ctx.injected[n:]),
          "an answer handed over by status is not injected again when the end frame arrives", row=row,
          injected=ctx.injected[n:])
    check(wait_for(lambda: plugin._live_observer(task_id) is None), "the observer closes after that turn end")
    task_id, adm = new_task()
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_timeout", 15)
    gw.done[adm] = "boom: the tool crashed"
    gw.failed.add(adm)
    gw.running = False
    row = plugin.status(task_id)["tasks"][0]
    n = len(ctx.injected)
    gw.emit("message.complete", text="boom: the tool crashed", status="error", admission_id=adm)
    time.sleep(1.0)
    check(row["status"] == "failed" and plugin.store.get(task_id)["status"] == "failed"
          and not any("boom" in t for t in ctx.injected[n:]),
          "a failure handed over by status is not injected again when the end frame arrives", row=row,
          injected=ctx.injected[n:])
    check(wait_for(lambda: plugin._live_observer(task_id) is None), "the observer closes after that failed turn end")
    task_id, adm = new_task()
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_timeout", 15)
    n = len(ctx.injected)
    gw.end_turn(adm, "answer after the limit")
    wait_for(lambda: len(ctx.injected) > n)
    injected = [t for t in ctx.injected[n:] if "answer after the limit" in t]
    check(len(injected) == 1 and plugin.store.get(task_id)["status"] == "answered",
          "past the wait limit with no status call, the turn's end injects the answer once", injected=ctx.injected[n:])

    # 16. Past the wait limit, a receipt whose turn ends with WAITING: is an update for status, not the answer.
    task_id, adm = new_task()
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_timeout", 15)
    gw.done[adm] = "half done\nWAITING: CI"
    gw.running = False
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "waiting_timeout" and "answer" not in row and row.get("waiting_on") == ["CI"],
          "status does not take a paused turn's receipt for the answer", row=row)
    n = len(ctx.injected)
    gw.emit("message.complete", text="half done\nWAITING: CI", status="complete", admission_id=adm)
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    time.sleep(0.5)
    check(plugin._live_observer(task_id) is not None and len(ctx.injected) == n,
          "the observer keeps following the paused task, and the update is not sent", injected=ctx.injected[n:])
    ctx.config.pop("wait_timeout_seconds")

    # 16b. The observer is busy until a turn's end is handled.
    seen = {}
    real_turn_ended = plugin._turn_ended

    async def spy_turn_ended(tid, frame, observer):
        seen.setdefault("busy", observer.busy)
        return await real_turn_ended(tid, frame, observer)

    plugin._turn_ended = spy_turn_ended
    observer = plugin._live_observer(task_id)
    gw.start_injected("inj-b")
    wait_for(lambda: observer.busy)
    gw.end_turn("inj-b", "more\nWAITING: CI")
    wait_for(lambda: "busy" in seen)
    del plugin._turn_ended
    check(seen.get("busy") is True and wait_for(lambda: not observer.busy),
          "the observer stays busy while a turn's end is handled", seen=seen)

    # 16c. A busy observer past its pause deadline still sleeps between checks.
    waits = {"n": 0}
    real_wait = observer._stop.wait

    async def counting_wait():
        waits["n"] += 1
        return await real_wait()

    observer._stop.wait = counting_wait
    observer.busy, observer.deadline = True, time.monotonic() - 1
    time.sleep(0.5)
    check(waits["n"] <= 3, "a busy observer past its deadline does not spin", waits=waits["n"])
    observer.busy, observer.deadline = False, None

    # 16d. correct during a turn started by an injection steers that turn.
    gw.start_injected("inj-c")
    wait_for(lambda: observer.turn_admission == "inj-c")
    calls = len(gw.calls)
    fixed = plugin.correct(task_id, "use the other branch")
    receipts = [p.get("admission_id") for _, m, p in gw.calls[calls:] if m == "prompt.receipt"]
    check(receipts == ["inj-c"] and "session.steer" in [m for _, m, _ in gw.calls[calls:]],
          "correct reads the receipt of the injected turn and steers it", receipts=receipts, result=fixed)
    gw.end_turn("inj-c", "done\nWAITING: CI")
    wait_for(lambda: not observer.busy)
    close_task(task_id)

    # 16e. An end status already handed over is not delivered again, decided under the store's lock.
    task_id, adm = new_task()
    close_task(task_id)
    plugin.store.update(task_id, status="answered", delivered=True, delivered_by="status", last_answer="x")
    n = len(ctx.injected)
    plugin._deliver(task_id, "Answer again", status="answered", last_answer="x")
    check(len(ctx.injected) == n and plugin.store.get(task_id)["delivered_by"] == "status",
          "the observer's end is dropped when status already recorded it", injected=ctx.injected[n:])

    # 16f. A blocking read that does not finish in time is an error, not an empty result.
    real_ack = mod.ACK_TIMEOUT_SECONDS
    mod.ACK_TIMEOUT_SECONDS = 0.2
    raised = ""
    try:
        plugin._run_blocking(asyncio.sleep(1.0))
    except TimeoutError as exc:
        raised = str(exc)
    finally:
        mod.ACK_TIMEOUT_SECONDS = real_ack
    check(bool(raised), "a read past the acknowledgement timeout raises", raised=raised)

    # 16g. Past the follow limit, a later turn that still waits is an update; the next one is the answer.
    ctx.config["follow_timeout_seconds"] = 2
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: CI")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "follow_lost", 15)
    wait_for(lambda: plugin._live_observer(task_id) is None)
    gw.messages = [{"role": "assistant", "text": "rerun started\nWAITING: CI"}]
    gw.running = False
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "follow_lost" and "answer" not in row and row.get("waiting_on") == ["CI"],
          "a later turn that still waits is an update, not the answer", row=row)
    gw.messages = [{"role": "assistant", "text": "CI green, merged"}]
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "answered" and row.get("answer") == "CI green, merged",
          "the next later turn is the answer", row=row)
    ctx.config.pop("follow_timeout_seconds")

    # 17. An approval with no raise time still ends with a notice.
    ended = {}
    for name in ("interrupted", "timeout", "ran"):
        try:
            text, record = plugin._ended_text("t-old", {"profile": "implementer", "session_id": "s1"},
                                              {"prompt_id": "p0", "command": "ls"}, (name, ""))
            ended[name] = (text, record["settled_after_s"])
        except Exception as exc:  # noqa: BLE001 - the failure is what the check reports
            ended[name] = repr(exc)
    check(all(isinstance(v, tuple) and "`ls`" in v[0] and v[1] is None for v in ended.values())
          and "expired unanswered (the implementer" in ended["timeout"][0],
          "an approval with no raise time ends with a notice and no duration", ended=ended)

    # 18. status running right after the end is written: nothing is injected after it hands the answer over.
    late_total, status_got = 0, 0
    for i in range(20):
        task_id, adm = new_task()
        close_task(task_id)
        answer = f"the answer {i}"
        ready, done = threading.Event(), threading.Event()
        box = {}

        def run_status(tid=task_id, ready=ready, done=done, box=box):
            ready.wait(5)
            box["row"] = plugin.status(tid)["tasks"][0]
            box["seen"] = len(ctx.injected)
            done.set()

        real_modify = plugin.store.modify
        fired = {"n": 0}

        def spy(tid, change, _real=real_modify, fired=fired, ready=ready, done=done):
            out = _real(tid, change)
            if out is not None and out.get("status") == "answered" and not fired["n"]:
                fired["n"] = 1
                ready.set()
                done.wait(2.0)
            return out

        plugin.store.modify = spy
        racer = threading.Thread(target=run_status, daemon=True)
        racer.start()
        n = len(ctx.injected)
        plugin._deliver(task_id, f"Answer from implementer: {answer}", status="answered", last_answer=answer)
        plugin.store.modify = real_modify
        racer.join(15)
        time.sleep(0.3)
        plugin._flush_outbox("retry")
        late_total += len([x for x in ctx.injected[box.get("seen", n):] if answer in x])
        status_got += (box.get("row") or {}).get("answer") == answer
    check(late_total == 0 and status_got == 20, "over 20 rounds, no answer is injected after status handed it over",
          late=late_total, status_handed_over=status_got)

    # 18d. status taking a queued answer marks it delivered, so a parent no longer counts the task as open.
    task_id, adm = new_task()
    close_task(task_id)
    ctx.accept = False
    plugin._deliver(task_id, "Answer from implementer: queued", status="answered", last_answer="queued")
    ctx.accept = True
    row = plugin.status(task_id)["tasks"][0]
    task = plugin.store.get(task_id)
    check(row.get("answer") == "queued" and row.get("delivered") is True and task.get("delivered") is True
          and task.get("delivered_by") == "status" and not task.get("outbox"),
          "status taking a queued answer records it delivered by status", row=row, task=task)
    task_id, adm = new_task()
    close_task(task_id)
    plugin._deliver(task_id, "Answer from implementer: injected", status="answered", last_answer="injected")
    wait_for(lambda: plugin.store.get(task_id).get("delivered") is True)
    plugin.status(task_id)
    check(plugin.store.get(task_id).get("delivered_by") == "injection",
          "an answer already injected keeps its injection record", task=plugin.store.get(task_id))
    # A newer turn ending after status read the task keeps its queued result and stays undelivered.
    task_id, adm = new_task()
    close_task(task_id)
    ctx.accept = False
    plugin._deliver(task_id, "Answer from implementer: older", status="answered", last_answer="older")
    ctx.accept = True
    snapshot = plugin.store.all()
    newer = mod._notice("Answer from implementer: newer", "result")
    plugin.store.modify(task_id, lambda t: {"turn_token": "newer-turn", "last_answer": "newer", "delivered": False,
                                            "delivered_by": None, "outbox": [newer]})
    real_all = plugin.store.all
    plugin.store.all = lambda: snapshot
    try:
        row = plugin.status(task_id)["tasks"][0]
    finally:
        plugin.store.all = real_all
    task = plugin.store.get(task_id)
    check(row.get("answer") == "older" and not task.get("delivered") and task.get("delivered_by") is None
          and [e["id"] for e in task.get("outbox") or []] == [newer["id"]],
          "status reading an earlier turn leaves a newer turn's queued result undelivered", row=row, task=task)
    # A newer turn ending between a refreshed read and the task's reload keeps its queued result.
    task_id, adm = new_task()
    close_task(task_id)
    plugin.store.modify(task_id, lambda t: {"status": "wait_lost"})
    newer = mod._notice("Answer from implementer: newer", "result")

    def refresh_then_newer_turn(task, tid=task_id, newer=newer):
        plugin.store.modify(tid, lambda t: {"turn_token": "newer-turn", "status": "answered", "last_answer": "newer",
                                            "delivered": False, "delivered_by": None, "outbox": [newer]})
        return {"answer": "older"}

    plugin._refresh = refresh_then_newer_turn
    try:
        row = plugin.status(task_id)["tasks"][0]
    finally:
        del plugin._refresh
    task = plugin.store.get(task_id)
    check(row.get("answer") == "older" and not task.get("delivered") and task.get("delivered_by") is None
          and [e["id"] for e in task.get("outbox") or []] == [newer["id"]],
          "a newer turn ending after a refreshed read keeps its queued result undelivered", row=row, task=task)

    # 18b. A paused task whose connection was cut reads the session's later turns, not the paused receipt.
    set_subtasks({})
    task_id, adm = new_task()
    gw.end_turn(adm, "waiting\nWAITING: CI")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "waiting_subwork")
    close_task(task_id)
    plugin.store.update(task_id, status="waiting_subwork")
    plugin._wait_cut(task_id, None, "connection reset")
    check(plugin.store.get(task_id)["status"] == "wait_lost", "the cut paused task is wait_lost",
          status=plugin.store.get(task_id)["status"])
    gw.done[adm] = "waiting\nWAITING: CI"
    gw.messages = [{"role": "assistant", "text": "CI green, finished", "timestamp": time.time() + 1}]
    gw.running = False
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "answered" and row.get("answer") == "CI green, finished",
          "status of a cut paused task returns the later turn that finished it", row=row)

    # 18c. Past the follow limit, a later turn with the same text as the paused one is still a new turn.
    ctx.config["follow_timeout_seconds"] = 2
    set_subtasks({"t-sub9": sub("running")})
    task_id, adm = new_task()
    gw.end_turn(adm, "Done")
    wait_for(lambda: plugin.store.get(task_id)["status"] == "follow_lost", 15)
    wait_for(lambda: plugin._live_observer(task_id) is None)
    set_subtasks({})
    told_at = plugin.store.get(task_id)["told_at"]
    gw.messages = [{"role": "assistant", "text": "Done", "timestamp": told_at - 1}]
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "follow_lost" and "answer" not in row, "the paused turn itself is not a new turn", row=row)
    plugin.store.update(task_id, told_at=None)
    gw.messages = [{"role": "assistant", "text": "Done", "timestamp": told_at + 1}]
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "follow_lost" and "answer" not in row,
          "a task stored with no told time compares the text, so the told turn is not told again", row=row)
    plugin.store.update(task_id, told_at=told_at)
    gw.messages = [{"role": "assistant", "text": "Done", "timestamp": told_at + 1}]
    row = plugin.status(task_id)["tasks"][0]
    check(row["status"] == "answered" and row.get("answer") == "Done",
          "a later turn with the same text is the answer", row=row)
    ctx.config.pop("follow_timeout_seconds")

    mod._Connection = real_connection
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
