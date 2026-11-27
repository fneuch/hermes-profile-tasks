"""An approval withdrawn while the gateway stops is reported as unknown, whatever the timing of the stop.

When the gateway stops it flags itself (``_running`` false, ``_draining`` true) before it interrupts any turn; the
interruption withdraws the approvals open in the turn. Such a withdrawal cannot be told from a person's answer that
arrived at the same moment, so whether the command ran is unknown, never "did not run". The plugin used to decide
that at the next load from the time between the withdrawal and the cut of the wait (STOP_GRACE_SECONDS, 5 s): a stop
that took longer, as one does on Windows under load, left the withdrawal reported as "did not run".

Checks (the stop flag faked, no gateway):
1. a withdrawal seen while stopping is recorded ``interrupted`` and flagged, and its notice says it is unknown;
2. at the next load the restart notice says whether the command ran is unknown, with the wait cut 0 s, 8 s and
   never (no wait_lost notice at all) after the withdrawal: the outcome does not depend on the timing;
3. a withdrawal with no stop (a correction, a person) stays ``cancelled`` and "did not run", also across a restart;
4. a definite outcome seen while stopping (the command ran, was denied, timed out) is kept;
5. a record from before this field (no flag) keeps the timing rule: withdrawn within the grace of the cut = stop;
6. the stop flag does not outlive the restart notice it was recorded for: after the recovery, the task waits again
   with a dead waiter and no new approval, and the next recovery does not report the old approval again, whichever
   of the two recovery paths (cut-off wait, paused task) handled the first and the second restart.

Run: ``PT_SCRIPT=tests/test_stop_cancel.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter). ``e2e/run.sh <path to __init__.py>`` with that script runs it against another copy of the plugin.
"""

from __future__ import annotations

import importlib.util
import json
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
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:900], flush=True)

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    dead = f"{gone.pid}:0"
    stopping = {"now": False}
    plugin._gateway_stopping = lambda: stopping["now"]

    def approval_task(task_id: str) -> str:
        """A running task with an approval open, as the observer records it."""
        plugin.store.update(task_id, **{
            "task_id": task_id, "profile": "analyst", "title": f"Task {task_id}", "status": "running",
            "session_id": f"s-{task_id}", "admission_id": "a1", "turns": ["a1"], "caller_session_key": "mgr",
            "waiter": mod._process_token(__import__("os").getpid()), "turn_token": "t1"})
        prompt = f"p-{task_id}"
        plugin._on_target_frame(task_id, {"type": "tool.start", "session_id": f"s-{task_id}",
                                          "payload": {"tool_id": "w", "name": "terminal"}})
        plugin._on_target_frame(task_id, {"type": "approval.request", "session_id": f"s-{task_id}", "payload": {
            "prompt_id": prompt, "command": "rm -rf probe", "description": "recursive delete", "timeout_s": 10}})
        return prompt

    def outbox_text(task_id: str, kind: str) -> str:
        return " ".join(e["text"] for e in (plugin.store.get(task_id) or {}).get("outbox") or [] if e["kind"] == kind)

    def end_approval(task_id: str, prompt: str, outcome: tuple[str, str], while_stopping: bool) -> str:
        """End the approval as the target's frames would; return what the calling conversation was told."""
        mark = len(ctx.injected)
        stopping["now"] = while_stopping
        try:
            plugin._approval_ended(task_id, prompt, outcome)
        finally:
            stopping["now"] = False
        return " ".join(text for text, _ in ctx.injected[mark:])

    def restart(task_id: str, cut_after: float | None) -> dict:
        """Stop the plugin's process (its waiter becomes dead), then load: optionally a wait_lost cut *cut_after*
        seconds after the withdrawal first."""
        task = plugin.store.get(task_id)
        fields: dict = {"waiter": dead}
        if cut_after is not None:
            last = task["last_approval"]
            queued = (last.get("settled_at") or time.time()) + cut_after
            fields["outbox"] = list(task.get("outbox") or []) + [
                {"id": "w1", "kind": "wait_lost", "text": "cut", "queued_at": queued, "attempts": []}]
        plugin.store.update(task_id, **fields)
        plugin.recover_after_restart()
        return plugin.store.get(task_id)

    # 1. Withdrawn while stopping.
    prompt = approval_task("t-stop")
    ended = end_approval("t-stop", prompt, ("cancelled", "turn interrupted"), True)
    last = plugin.store.get("t-stop").get("last_approval") or {}
    check(last.get("outcome") == "interrupted" and last.get("while_stopping") is True
          and "whether it ran is unknown" in ended and "did not run" not in ended,
          "an approval withdrawn while the gateway stops is recorded interrupted and flagged; its notice says "
          "whether the command ran is unknown", last_approval=last, notice=ended)

    # 2. The next load, whatever the time between the withdrawal and the cut of the wait.
    for label, cut_after in (("the wait cut 0 s later", 0.0), ("the wait cut 8 s later (past the 5 s grace)", 8.0),
                             ("the wait cut 90 s later", 90.0), ("the wait never cut", None)):
        task_id = f"t-stop-{cut_after}"
        prompt = approval_task(task_id)
        end_approval(task_id, prompt, ("cancelled", "turn interrupted"), True)
        after = restart(task_id, cut_after)
        text = outbox_text(task_id, "restart")
        check("whether the command ran is unknown" in text and (after.get("last_approval") or {}).get("outcome")
              == "interrupted", f"at the next load the restart notice says whether the command ran is unknown, {label}",
              notice=text[-200:], last_approval_outcome=(after.get("last_approval") or {}).get("outcome"))

    # 3. A withdrawal with no stop stays a withdrawal, also across a restart.
    prompt = approval_task("t-plain")
    ended = end_approval("t-plain", prompt, ("cancelled", "turn interrupted"), False)
    last = plugin.store.get("t-plain").get("last_approval") or {}
    after = restart("t-plain", None)
    text = outbox_text("t-plain", "restart")
    check(last.get("outcome") == "cancelled" and not last.get("while_stopping")
          and "did not run" in ended and "unknown" not in text
          and (after.get("last_approval") or {}).get("outcome") == "cancelled",
          "a withdrawal with no stop stays cancelled and 'did not run', also across a restart",
          last_approval=last.get("outcome"), notice=ended[:160], restart=text[-160:])

    # 4. A definite outcome seen while stopping is kept.
    for outcome, wording in ((("ran", ""), "the command ran"), (("denied", "denied"), "the command did not run"),
                             (("timeout", ""), "the command did not run")):
        task_id = f"t-definite-{outcome[0]}"
        prompt = approval_task(task_id)
        ended = end_approval(task_id, prompt, outcome, True)
        last = plugin.store.get(task_id).get("last_approval") or {}
        check(last.get("outcome") == outcome[0] and wording in ended,
              f"an approval that ended '{outcome[0]}' while the gateway stops keeps its outcome",
              outcome=last.get("outcome"), notice=ended[:160])

    # 5. A record from before the flag keeps the timing rule.
    now = time.time()
    approval = {"prompt_id": "p", "command": "x", "raised_at": now - 3, "settled_at": now - 2, "outcome": "cancelled",
                "settled_after_s": 1.0}
    cut = lambda after: {"outbox": [{"kind": "wait_lost", "queued_at": now - 2 + after}]}  # noqa: E731
    check(mod._ended_by_stop(cut(1.0), approval) and not mod._ended_by_stop(cut(8.0), approval)
          and not mod._ended_by_stop({}, approval)
          and mod._ended_by_stop({}, approval | {"while_stopping": True})
          and not mod._ended_by_stop({}, approval | {"while_stopping": True, "outcome": "ran"}),
          "_ended_by_stop: a flagged cancelled/interrupted record is a stop at any timing; an unflagged one keeps "
          "the 5 s rule; a definite outcome is never one")

    # 6. The flag ends with the notice it caused: a later restart does not repeat the old approval.
    for first, second in (("running", "running"), ("running", "waiting_subwork"),
                          ("waiting_subwork", "waiting_subwork"), ("waiting_subwork", "running")):
        task_id = f"t-again-{first}-{second}"
        prompt = approval_task(task_id)
        end_approval(task_id, prompt, ("cancelled", "turn interrupted"), True)
        plugin.store.update(task_id, status=first, pause_reasons=["a command"], waiter=dead)
        plugin.recover_after_restart()
        task = plugin.store.get(task_id)
        first_text = outbox_text(task_id, "restart")
        recovered = task.get("last_approval") or {}
        # Back to waiting, its process gone again, nothing new asked: the follow-up of a recovered task.
        plugin.store.update(task_id, status=second, pause_reasons=["a command"], waiter=dead, error=None)
        before = len(plugin.store.get(task_id).get("outbox") or [])
        plugin.recover_after_restart()
        again = plugin.store.get(task_id)
        second_notices = " ".join(e["text"] for e in (again.get("outbox") or [])[before:] if e["kind"] == "restart")
        check(bool("whether the command ran is unknown" in first_text and "probe" in first_text
                   and not recovered.get("while_stopping") and second_notices
                   and "probe" not in second_notices and "unknown" not in second_notices
                   and (again.get("last_approval") or {}).get("outcome") == "interrupted"),
              f"a second restart does not repeat the old approval (first {first}, then {second})",
              first_notice=first_text[-160:], second_notice=second_notices[-200:],
              recovered_flag=recovered.get("while_stopping"))

    print("PASS" if not failures else "FAIL", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
