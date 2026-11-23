"""profile-tasks backend, mounted at /api/plugins/profile-tasks/.

GET /tasks?session_id=<stored session id>&session_profile=<profile> returns the profile_task tasks started from
that conversation, newest first, for the desktop status-bar item (desktop/plugin.js). The conversation's profile
is named in the query because the tasks live in that profile's plugin state, whichever profile the request is
scoped to. The state file is read as it is on disk: nothing is written, no lock is taken, no target is called
(the tool's action=status refreshes and records, this route does not).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

PLUGIN_ID = "profile-tasks"
STATE_KEY = "tasks"
ACTIVE = ("starting", "running", "waiting_timeout")


def state_path(profile: str) -> Path:
    """The plugin state file of *profile*; ValueError for a name that is not a profile id."""
    from hermes_cli.plugins_state import PluginState
    from hermes_cli.profiles import get_profile_dir
    # PluginState gives the plugin's data folder name; its data_dir would be the request profile's.
    return get_profile_dir(profile) / "plugin-data" / PluginState(PLUGIN_ID).data_dir.name / "state.json"


def read_tasks(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    tasks = data.get(STATE_KEY) if isinstance(data, dict) else None
    return tasks if isinstance(tasks, dict) else {}


def task_state(task: dict) -> str:
    """``approval``, ``running``, ``past_wait_limit``, ``wait_lost``, ``answered``, ``late_answer`` or ``failed``.

    A late answer is one read after the wait ended (wait limit or gateway restart): the tool's action=status
    records it with ``delivered_by: status``.
    """
    status = task.get("status")
    if task.get("pending_approval") and status in ACTIVE:
        return "approval"
    if status in ("starting", "running"):
        return "running"
    if status == "waiting_timeout":
        return "past_wait_limit"
    if status == "answered":
        return "late_answer" if task.get("delivered_by") == "status" else "answered"
    return status or "unknown"


def rows_for_session(tasks: dict[str, dict], session_id: str) -> list[dict[str, Any]]:
    """The tasks whose calling conversation is *session_id*, newest first; keyed tasks have none."""
    rows = []
    for task in tasks.values():
        if not isinstance(task, dict) or task.get("key") or task.get("caller_session_id") != session_id:
            continue
        approval = task.get("pending_approval") or {}
        rows.append({
            "task_id": task.get("task_id"),
            "profile": task.get("profile"),
            "title": task.get("title"),
            "status": task.get("status"),
            "state": task_state(task),
            "session_id": task.get("session_id"),
            "approval_command": approval.get("command") if isinstance(approval, dict) else None,
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        })
    return sorted(rows, key=lambda r: r.get("created_at") or 0, reverse=True)


@router.get("/tasks")
def tasks(session_id: str = Query(""), session_profile: str = Query("default")) -> dict:
    if not session_id:
        return {"tasks": [], "at": time.time()}
    try:
        path = state_path(session_profile or "default")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        found = read_tasks(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"cannot read {path.name}: {exc}") from exc
    return {"tasks": rows_for_session(found, session_id), "at": time.time()}
