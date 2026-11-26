"""usage-governor pre_tool_call hook: blocks task starts and disallowed delegation options by mode.

Each call re-reads the default profile's state.json (mode and the tick's return time); no cache. Only
`enforce` and `reduced_options` come from settings, of the profile the hook runs in. A missing state, a
wrong setting or any error blocks nothing (logged once). README.md, section Enforcement, gives the rules.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import governor

logger = logging.getLogger(__name__)

NO_START_MODES = ("quiet", "budget", "essential")
OPTION_MODES = ("quiet", "budget", "essential", "reduced")
_logged: set[str] = set()


@dataclass(frozen=True)
class Enforcement:
    enforce: bool
    options: tuple[str, ...]


def read_enforcement(values: Mapping, defaults: Mapping) -> Enforcement:
    """`enforce` and `reduced_options` from *values* over *defaults*. Raises SettingsError."""
    merged = {**defaults, **{k: v for k, v in values.items() if v is not None}}
    enforce = merged.get("enforce")
    if not isinstance(enforce, bool):
        raise governor.SettingsError(f"enforce should be true or false, not {enforce!r}")
    options = merged.get("reduced_options")
    if not isinstance(options, list) or not all(isinstance(o, str) and o.strip() for o in options):
        raise governor.SettingsError(f"reduced_options should be a list of option names, not {options!r}")
    return Enforcement(enforce=enforce, options=tuple(o.strip() for o in options))


def enforcement(values: Callable[[], Mapping] = governor.configured,
                defaults: Callable[[], Mapping] = governor.schema_defaults) -> Enforcement:
    """The enforcement settings, read again at each call. Raises SettingsError."""
    try:
        got, base = values(), defaults()
    except Exception as exc:  # noqa: BLE001 - an unreadable config.yaml or plugin.yaml is a settings error
        raise governor.SettingsError(f"settings unreadable: {type(exc).__name__}: {exc}") from exc
    return read_enforcement(got, base)


def _log_once(text: str, exc_info: bool = False) -> None:
    if text not in _logged:
        _logged.add(text)
        logger.error("usage-governor: %s", text, exc_info=exc_info)


def _calls(tool_name: Any, args: Any) -> list[tuple[str, dict]]:
    """`[(name, args)]`; a tool_call wrapper that reaches the hook is opened (Hermes normally unwraps it)."""
    name = str(tool_name or "")
    args = args if isinstance(args, dict) else {}
    if name != "tool_call":
        return [(name, args)]
    if isinstance(args.get("calls"), list):
        entries = args["calls"]
    else:
        entries = [args]
    return [(str(e.get("name") or ""), _arguments(e.get("arguments"))) for e in entries if isinstance(e, dict)]


def _arguments(value: Any) -> dict:
    """A wrapped call's arguments; tool_call accepts them as a JSON string too. Unparsable = no args."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def refusal(name: str, args: dict, mode: str, allowed: tuple[str, ...]) -> Optional[str]:
    """What is refused in *mode*, or None."""
    if mode not in OPTION_MODES:
        return None
    allowed_text = ", ".join(allowed) or "none"
    unnamed = (f"{name} without an option is not allowed in {mode} mode; name one of {allowed_text}: "
               "the default option is not visible to the governor")
    if name == "profile_task":
        if str(args.get("action") or "").strip() == "start" and mode in NO_START_MODES:
            return f"profile_task start is not allowed in {mode} mode"
        if "option" in args:
            option = args.get("option")
            if not isinstance(option, str) or not option.strip():
                return unnamed
            if option.strip() not in allowed:
                return f"profile_task: option {option.strip()} is not allowed in {mode} mode; allowed: {allowed_text}"
        return None
    if name == "delegate_task":
        # Hermes lowercases the action before dispatch (tools/delegate_tool.py).
        if str(args.get("action") or "spawn").strip().lower() != "spawn":
            return None
        tasks = args.get("tasks")
        if isinstance(tasks, str):
            # delegate_task accepts tasks as a JSON string (tools/delegate_tool_tasks.py); unparsable = unnamed.
            try:
                tasks = json.loads(tasks)
            except ValueError:
                return unnamed
        if not isinstance(tasks, list) or not tasks:
            return unnamed
        for task in tasks:
            option = task.get("option") if isinstance(task, dict) else None
            if not isinstance(option, str) or not option.strip():
                return unnamed
            if option.strip() not in allowed:
                return f"delegate_task: option {option.strip()} is not allowed in {mode} mode; allowed: {allowed_text}"
    return None


def back_at(state: Mapping, now: float) -> Optional[str]:
    """The tick's `until_local`, or None when the state has none (older tick, full) or its `until` is past."""
    text = state.get("until_local")
    if not isinstance(text, str) or not text.strip():
        return None
    until = state.get("until")
    if isinstance(until, (int, float)) and not isinstance(until, bool) and until < now:
        return None
    return text.strip()


def load_state(data: Path) -> Optional[dict]:
    """state.json when it has a known mode, else None (logged once)."""
    path = data / governor.STATE
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = None
    if isinstance(state, dict) and state.get("mode") in governor.MODES:
        return state
    _log_once(f"no usable state at {path}, nothing blocked")
    return None


def decide(tool_name: Any, args: Any, now: Optional[float] = None, data: Optional[Path] = None,
           read_enforcement: Callable[[], Enforcement] = enforcement) -> Optional[dict]:
    """The block directive for this call, or None."""
    calls = [c for c in _calls(tool_name, args) if c[0] in ("profile_task", "delegate_task")]
    if not calls:
        return None
    now = time.time() if now is None else now
    try:
        rules = read_enforcement()
    except governor.SettingsError as exc:
        _log_once(f"enforcement off, a setting is wrong: {exc}")
        return None
    if not rules.enforce:
        return None
    state = load_state(Path(data) if data is not None else governor.data_dir())
    if state is None:
        return None
    mode = state["mode"]
    for name, call_args in calls:
        refused = refusal(name, call_args, mode, rules.options)
        if refused:
            back = back_at(state, now)
            until = f" Expected back at {back} (local)." if back else ""
            return {"action": "block", "message": f"BLOCKED by usage-governor: {refused}.{until}"}
    return None


def check(tool_name: Any = None, args: Any = None, **kwargs: Any) -> Optional[dict]:
    """The pre_tool_call hook: any error blocks nothing."""
    try:
        return decide(tool_name, args)
    except Exception as exc:  # noqa: BLE001 - the hook never breaks or blocks a call on its own error
        _log_once(f"enforcement hook failed, nothing blocked: {type(exc).__name__}: {exc}", exc_info=True)
        return None
