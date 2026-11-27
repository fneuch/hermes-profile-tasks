"""register() stays far inside Hermes' plugin load limit (plugins.load_timeout_seconds, 10 s by default), whatever the
restart recovery has to do and however loaded the machine is.

Past that limit Hermes drops the plugin and the ``profile_task`` tool is missing until the next gateway start. The
restart recovery used to run inside register(): it locks and rereads the whole task state for each task, waits for
the state lock of every other process that loads the plugin at the same moment (the gateway and the launcher that
attaches to it), and grows with the task history. Now register() only starts it in the background.

Checks:
1. Several cold processes load the plugin at once, over a state with a long history and many cut-off tasks, while
   every core is busy: each register() returns under the budget, and the recovery, finished in the background,
   still gives each cut-off task exactly one restart notice and no stale waiter.
2. A recovery that takes far longer than the load limit does not hold register() back; the first tool call waits
   for it, but never longer than RECOVERY_WAIT_SECONDS.
3. A recovery that raises leaves the tool registered and usable.
4. recover_after_restart reads and writes the state a few times, not once per task: every state access waits for
   the state lock of the other loading processes (Hermes polls it once a second on Windows and gives up after
   ten tries, with ``OSError: [Errno 36] Resource deadlock avoided``, which the plugin's old per-task passes hit).

Run: ``PT_SCRIPT=tests/test_register_budget.py e2e/run.sh [rounds] [processes] [plugin __init__.py] [options]``
(isolated HOME/HERMES_HOME, the installed Hermes interpreter). Options (``e2e/run.sh`` clears the environment, so
they are flags): ``--history=N`` (finished tasks in the state, default 400), ``--cut=N`` (cut-off tasks, default
40), ``--budget=S`` (the register() budget in seconds, default 5: what remains is the cold import of the Hermes
modules the load check needs, 2 to 4 s when every core is busy), ``--burners=N`` (busy processes, default one per
core). Against the plugin before the fix (a copy passed as the third argument) checks 1 and 2 fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(os.environ.get("PT_PLUGIN") or Path(__file__).resolve().parents[1] / "__init__.py")


def _option(name: str, default: str, cast):
    """``--name=value`` on the command line (e2e/run.sh clears the environment), else PT_BUDGET_<NAME>."""
    for arg in sys.argv[1:]:
        if arg.startswith(f"--{name}="):
            return cast(arg.split("=", 1)[1])
    return cast(os.environ.get(f"PT_BUDGET_{name.upper()}", default))


HISTORY = _option("history", "400", int)
CUT = _option("cut", "40", int)
BUDGET = _option("budget", "5", float)
BURNERS = _option("burners", str(os.cpu_count() or 2), int)


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
        self.tools: list[dict] = []
        self.hooks: list[str] = []

    def get_config(self, key, default=None):
        return default

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def on_unload(self, callback):
        pass

    def inject_message(self, text, session_key=None):
        return False


def child(start_at: float) -> None:
    """One loading process: cold import, then register(), timed the way Hermes times it (import + register)."""
    began = time.perf_counter()
    mod = load()
    imported = time.perf_counter() - began
    spent: dict[str, float] = {}

    def timed(name, fn):
        def wrapper(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                spent[name] = time.perf_counter() - t
        return wrapper

    mod.check_internals = timed("internals", mod.check_internals)
    mod.ProfileTasks.recover_after_restart = timed("recovery", mod.ProfileTasks.recover_after_restart)
    time.sleep(max(0.0, start_at - time.time()))
    ctx = FakeCtx()
    t = time.perf_counter()
    mod.register(ctx)
    registered = time.perf_counter() - t
    plugin = mod._PLUGIN
    recovered = plugin.wait_recovered(120) if hasattr(plugin, "wait_recovered") else True
    print("RESULT " + json.dumps({"import_s": round(imported, 2), "register_s": round(registered, 2),
                                   "load_s": round(imported + registered, 2), "spent": {k: round(v, 2) for k, v in spent.items()},
                                   "tool_registered": bool(ctx.tools), "recovered": recovered}), flush=True)


def seed(ctx: FakeCtx, dead: str) -> list[str]:
    """A long history of finished tasks and CUT tasks a stopped gateway left waiting; returns the cut-off ids."""
    notices = [{"id": f"n{i}", "kind": "result", "text": "x" * 120, "queued_at": 1.0, "attempts": [],
                "delivered_at": 2.0} for i in range(20)]
    base = {"profile": "analyst", "caller_session_key": "agent:main:local:dm:x", "turns": ["a"], "outbox": [],
            "notices": notices}
    tasks = {f"t-old{i}": base | {"task_id": f"t-old{i}", "status": "answered", "session_id": f"s-old{i}",
                                  "admission_id": "a", "waiter": None, "last_answer": "y" * 400}
             for i in range(HISTORY)}
    cut = []
    for i in range(CUT):
        task_id = f"t-cut{i}"
        cut.append(task_id)
        approval = {"prompt_id": f"p{i}", "execution_generation": 1, "command": "rm -rf probe", "description": "d",
                    "choices": ["once", "deny"], "timeout_s": 10.0, "raised_at": time.time(),
                    "expires_at": time.time() + 10}
        tasks[task_id] = base | {"task_id": task_id, "status": "running", "session_id": f"s-cut{i}", "admission_id": "a",
                                 "waiter": dead} | ({"pending_approval": approval} if i % 2 else {})
    ctx.state.set("tasks", tasks)
    return cut


def burn(count: int) -> list[subprocess.Popen]:
    return [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(count)]


def main(argv: list[str]) -> int:
    if argv[:1] == ["--child"]:
        child(float(argv[1]))
        return 0
    global PLUGIN
    argv = [a for a in argv if not a.startswith("--")]
    rounds = int(argv[0]) if argv else 3
    procs = int(argv[1]) if len(argv) > 1 else 3
    if len(argv) > 2:
        # The loading processes read it from the environment; checks 2-4 load it here.
        os.environ["PT_PLUGIN"] = str(Path(argv[2]).resolve())
        PLUGIN = Path(argv[2]).resolve()
    print("plugin:", os.environ.get("PT_PLUGIN") or PLUGIN, flush=True)
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:900], flush=True)

    ctx = FakeCtx()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    dead = f"{gone.pid}:0"

    # 1. Cold processes loading at once, over a long history, every core busy.
    for n in range(rounds):
        cut = seed(ctx, dead)
        burners = burn(BURNERS)
        try:
            start_at = time.time() + 2.0
            children = [subprocess.Popen([sys.executable, __file__, "--child", str(start_at)],
                                         stdout=subprocess.PIPE, text=True) for _ in range(procs)]
            outputs = [proc.communicate(timeout=300)[0] for proc in children]
        finally:
            for proc in burners:
                proc.kill()
        results = [json.loads(line[len("RESULT "):]) for out in outputs for line in out.splitlines()
                   if line.startswith("RESULT ")]
        tasks = ctx.state.get("tasks")
        wrong = []
        for task_id in cut:
            task = tasks[task_id]
            restarts = [e for e in task.get("outbox") or [] if e.get("kind") == "restart"]
            if len(restarts) != 1 or task.get("waiter") is not None or task.get("status") != "wait_lost":
                wrong.append((task_id, len(restarts), task.get("waiter"), task.get("status")))
        slowest = max((r["load_s"] for r in results), default=None)
        check(len(results) == procs and all(r["tool_registered"] and r["recovered"] for r in results)
              and not wrong and slowest is not None and max(r["register_s"] for r in results) < BUDGET,
              f"round {n + 1}: {procs} processes load at once over {HISTORY + CUT} tasks ({CUT} cut off), "
              f"{BURNERS} busy processes: register() under {BUDGET} s, each cut-off task closed once",
              results=results, wrong=wrong[:5], exit_codes=[p.returncode for p in children])

    # 2-4 in this process, the recovery made slow, failing, or counted.
    mod = load()
    real_recover = mod.ProfileTasks.recover_after_restart

    def slow(seconds):
        def recover(self):
            time.sleep(seconds)
            return []
        return recover

    mod.ProfileTasks.recover_after_restart = slow(6.0)
    slow_ctx = FakeCtx()
    t = time.perf_counter()
    mod.register(slow_ctx)
    registered = time.perf_counter() - t
    plugin = mod._PLUGIN
    still_running = not plugin.wait_recovered(0.05) if hasattr(plugin, "wait_recovered") else False
    t = time.perf_counter()
    reply = slow_ctx.tools[0]["handler"]({"action": "status"}) if slow_ctx.tools else None
    answered_after = time.perf_counter() - t
    check(bool(registered < 3.0 and still_running and slow_ctx.tools and registered + answered_after >= 5.5
               and answered_after < 10.0),
          "a recovery of 6 s does not hold register() back (it returns in under 3 s with the tool registered, the "
          "recovery still running); the first tool call waits for it",
          register_s=round(registered, 2), recovery_running_after_register=still_running,
          first_call_s=round(answered_after, 2), reply=str(reply)[:120])

    mod.ProfileTasks.recover_after_restart = slow(30.0)
    mod.RECOVERY_WAIT_SECONDS = 1.0
    cap_ctx = FakeCtx()
    mod.register(cap_ctx)
    t = time.perf_counter()
    cap_ctx.tools[0]["handler"]({"action": "status"})
    capped = time.perf_counter() - t
    check(0.8 < capped < 5.0, "a recovery that never ends delays a tool call by RECOVERY_WAIT_SECONDS only",
          call_s=round(capped, 2))
    mod.RECOVERY_WAIT_SECONDS = 10

    def failing(self):
        raise RuntimeError("state unreadable")

    mod.ProfileTasks.recover_after_restart = failing
    fail_ctx = FakeCtx()
    try:
        mod.register(fail_ctx)
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = repr(exc)
    answered = fail_ctx.tools[0]["handler"]({"action": "status"}) if fail_ctx.tools else None
    check(raised is None and fail_ctx.tools and answered is not None,
          "a recovery that raises leaves the tool registered and usable", raised=raised, reply=str(answered)[:120])

    # 4. Finished tasks are skipped from one read: no lock round for each of them.
    mod.ProfileTasks.recover_after_restart = real_recover
    count_ctx = FakeCtx()
    cut = seed(count_ctx, dead)
    plugin = mod.ProfileTasks(count_ctx, [])
    rounds_on_state: list[str] = []
    state = count_ctx.state
    real_get, real_set = state.get, state.set
    state.get = lambda *a, **k: (rounds_on_state.append("get"), real_get(*a, **k))[1]
    state.set = lambda *a, **k: (rounds_on_state.append("set"), real_set(*a, **k))[1]
    found = plugin.recover_after_restart()
    state.get, state.set = real_get, real_set
    check(sorted(found) == sorted(cut) and len(rounds_on_state) <= 8,
          f"recover_after_restart reads and writes the state a few times, not once per task "
          f"({len(rounds_on_state)} state calls for {HISTORY + CUT} tasks), and closes every cut-off one",
          found=len(found), state_calls=len(rounds_on_state), tasks=HISTORY + CUT)

    print("PASS" if not failures else "FAIL", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
