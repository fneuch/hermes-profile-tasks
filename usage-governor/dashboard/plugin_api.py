"""usage-governor backend, mounted at /api/plugins/usage-governor/.

GET /state returns `{"state": <state.json or null>, "health": {"ok": bool, "reasons": [...]}}` for the desktop
status-bar item (desktop/plugin.js). It reads the default profile's plugin-data/usage-governor through
governor.data_dir() and governor.health(); it writes nothing and answers 200 even when the state is missing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()

GOVERNOR_FILE = Path(__file__).resolve().parents[1] / "governor.py"
MODULE_NAME = "usage_governor_governor_for_api"


def governor():
    """The plugin's governor.py, loaded once by path (the dashboard imports this file alone)."""
    mod = sys.modules.get(MODULE_NAME)
    if mod is None:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, GOVERNOR_FILE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(MODULE_NAME, None)
            raise
    return mod


def read_state(path: Path) -> Any:
    """state.json as a dict, or None when it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text())  # same encoding as governor.py reads it with
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@router.get("/state")
def get_state() -> dict[str, Any]:
    try:
        g = governor()
        data = g.data_dir()
    except Exception as exc:  # noqa: BLE001 - the route answers with the reason, never a 500
        return {"state": None, "health": {"ok": False, "reasons": [f"governor unavailable: {type(exc).__name__}: {exc}"]}}
    try:
        ok, reasons = g.health(data=data)
    except Exception as exc:  # noqa: BLE001 - same
        ok, reasons = False, [f"health check failed: {type(exc).__name__}: {exc}"]
    return {"state": read_state(data / g.STATE), "health": {"ok": ok, "reasons": list(reasons)}}
