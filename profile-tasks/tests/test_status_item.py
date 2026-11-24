"""The desktop status-bar item's data: the ``caller_session_id`` field and the read-only ``GET /tasks`` route.

The route (dashboard/plugin_api.py) lists the tasks a conversation started, by that field, with their state;
it must leave the plugin state file as it found it. A task save sends ``plugin.profile-tasks.tasks.changed``.
The target gateway is replaced by a fake ``_send``.

Run: ``PT_SCRIPT=tests/test_status_item.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes interpreter).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
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
        return True


def main() -> int:
    mod = load(HERE / "__init__.py", "profile_tasks_under_test")
    api = load(HERE / "dashboard" / "plugin_api.py", "profile_tasks_api_under_test")
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    from gateway.session_context import clear_session_vars, set_session_vars

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])
    mod._PLUGIN = plugin
    plugin.targets = lambda: ["analyst", "reviewer"]
    plugin._send = lambda task_id, text, *, create: {"task_id": task_id, "accepted": True}
    plugin._running_turn = lambda task, read=None: None
    plugin._read_turn = lambda task: None

    # The new field: the calling conversation's durable session id, set at start and on a follow-up from it.
    tokens = set_session_vars(session_key="key-A", session_id="conv-A", source="desktop")
    try:
        started = plugin.start("analyst", "do A", "first")["task_id"]
    finally:
        clear_session_vars(tokens)
    keyed = mod.send("k1", "reviewer", "keyed text")["task_id"]
    check(plugin.store.get(started).get("caller_session_id") == "conv-A"
          and plugin.store.get(started).get("caller_session_key") == "key-A"
          and plugin.store.get(keyed).get("caller_session_id") is None,
          "start records the calling conversation's session id; a keyed task has none",
          started=plugin.store.get(started), keyed=plugin.store.get(keyed))

    plugin.store.update(started, session_id="s-analyst", status="answered", turn_token="t0")
    tokens = set_session_vars(session_key="key-B", session_id="conv-B", source="desktop")
    try:
        plugin.follow_up(started, "more")
    finally:
        clear_session_vars(tokens)
    check(plugin.store.get(started).get("caller_session_id") == "conv-B",
          "a follow-up from another conversation moves the task to it, as its answer goes there",
          task=plugin.store.get(started))

    # Derived states.
    cases = {
        "approval": {"status": "running", "pending_approval": {"command": "rm -rf x"}},
        "running": {"status": "starting"},
        "past_wait_limit": {"status": "waiting_timeout"},
        "wait_lost": {"status": "wait_lost"},
        "answered": {"status": "answered", "delivered_by": "injection"},
        "late_answer": {"status": "answered", "delivered_by": "status"},
        "failed": {"status": "failed"},
    }
    got = {want: api.task_state(task) for want, task in cases.items()}
    check(all(want == state for want, state in got.items()), "each stored status maps to the item's state; an "
          "answer read through status after the wait ended is a late answer; an open approval wins", got=got)
    check(api.task_state({"status": "waiting_timeout", "pending_approval": {"command": "x"}}) == "approval"
          and api.task_state({"status": "waiting_subwork", "pending_approval": {"command": "x"}}) == "approval"
          and api.task_state({"status": "answered", "pending_approval": {"command": "x"}}) == "answered",
          "an approval counts only while the turn may still run")

    # Filtering by conversation.
    tasks = {
        "t-1": {"task_id": "t-1", "profile": "analyst", "title": "Task t-1: one", "status": "running",
                "caller_session_id": "conv-X", "session_id": "s1", "created_at": 1},
        "t-2": {"task_id": "t-2", "profile": "reviewer", "title": "Task t-2: two", "status": "answered",
                "delivered_by": "status", "caller_session_id": "conv-X", "session_id": "s2", "created_at": 2},
        "t-3": {"task_id": "t-3", "profile": "analyst", "title": "other conversation", "status": "running",
                "caller_session_id": "conv-Y", "created_at": 3},
        "t-4": {"task_id": "t-4", "profile": "analyst", "title": "older task, no field", "status": "answered",
                "caller_session_key": "conv-X", "created_at": 4},
        "t-5": {"task_id": "t-5", "profile": "analyst", "title": "keyed", "status": "running", "key": "k",
                "caller_session_id": "conv-X", "created_at": 5},
    }
    rows = api.rows_for_session(tasks, "conv-X")
    check([r["task_id"] for r in rows] == ["t-2", "t-1"] and rows[0]["state"] == "late_answer"
          and rows[0]["profile"] == "reviewer" and rows[0]["session_id"] == "s2",
          "only the conversation's own tasks, newest first; other conversations, tasks without the field and "
          "keyed tasks are left out", rows=rows)

    # The route reads the named profile's state and changes nothing on disk.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    path = Path(ctx.state.path)
    folder = path.parent
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in folder.iterdir()))
    body = client.get("/tasks", params={"session_id": "conv-B", "session_profile": "default"}).json()
    after = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in folder.iterdir()))
    check([t["task_id"] for t in body["tasks"]] == [started] and before == after
          and path == api.state_path("default"),
          "GET /tasks lists the conversation's tasks from the profile's own state file and leaves the folder "
          "byte for byte as it was", tasks=body["tasks"], files=after[2])

    analyst_home = Path(os.environ["HERMES_HOME"]) / "profiles" / "analyst"
    analyst_state = api.state_path("analyst")
    analyst_state.parent.mkdir(parents=True, exist_ok=True)
    analyst_state.write_text(json.dumps({"tasks": {"t-9": {"task_id": "t-9", "profile": "reviewer", "title": "x",
                                                            "status": "running", "caller_session_id": "conv-B"}}}))
    other = client.get("/tasks", params={"session_id": "conv-B", "session_profile": "analyst"}).json()
    empty = client.get("/tasks", params={"session_id": ""}).json()
    missing = client.get("/tasks", params={"session_id": "conv-B", "session_profile": "nobody"}).json()
    bad = client.get("/tasks", params={"session_id": "conv-B", "session_profile": "../x"})
    check([t["task_id"] for t in other["tasks"]] == ["t-9"] and str(analyst_state).startswith(str(analyst_home))
          and empty["tasks"] == [] and missing["tasks"] == [] and bad.status_code == 400,
          "session_profile picks the profile whose state is read; no session or a profile with no state lists "
          "nothing; a malformed profile name is refused", other=other, missing=missing, bad=bad.status_code)

    # Every save announces the change, in a process that serves desktop clients only.
    import hermes_cli.plugin_events as events
    sent = []
    real = events.broadcast_plugin_event
    events.broadcast_plugin_event = lambda plugin_id, event, payload=None: sent.append((plugin_id, event, payload))
    stub = "tui_gateway.server" not in sys.modules
    try:
        if stub:
            sys.modules["tui_gateway.server"] = types.ModuleType("tui_gateway.server")
        plugin.store.update(started, title="renamed")
        if stub:
            del sys.modules["tui_gateway.server"]
            plugin.store.update(started, title="renamed again")
    finally:
        events.broadcast_plugin_event = real
        if stub:
            sys.modules.pop("tui_gateway.server", None)
    check(sent[:1] == [("profile-tasks", "tasks.changed", {"task_id": started})] and len(sent) == (1 if stub else 2),
          "a task save sends plugin.profile-tasks.tasks.changed where the gateway server runs, nothing elsewhere",
          sent=sent, gateway_loaded_before=not stub)

    print("FAILURES", failures, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
