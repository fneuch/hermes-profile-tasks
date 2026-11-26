"""Usage governor (no model): reads the plan usage, decides the agents' mode and keeps its files.

The rules and values are the upstream governor.py as is; README.md documents the files.
Modes, most restrictive first: quiet, budget, essential, reduced, full.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLUGIN_ID = "usage-governor"
PLUGIN_DIR = Path(__file__).resolve().parent
MANIFEST = PLUGIN_DIR / "plugin.yaml"
MODES = ("quiet", "budget", "essential", "reduced", "full")
USAGE_COMMAND = ("hermes", "usage", "--json")
USAGE_TIMEOUT_S = 15
# A reading older than this counts as missing (Hermes declares its usage cache stale after 900 s).
STALE_S = 900
HEARTBEAT_MAX_AGE_S = 600
SESSION_LABEL = "Current session"
WEEK_LABEL = "Current week"
DAY_S = 86400
STATE, JOURNAL, OVERRIDE, DAY, HEARTBEAT = "state.json", "journal.jsonl", "override.json", "day.json", "heartbeat"


class SettingsError(ValueError):
    """A setting that keeps the tick from running; the text names the setting."""


@dataclass(frozen=True)
class Settings:
    tz: ZoneInfo
    quiet: Optional[tuple[int, int]]
    review: Optional[int]
    daily_percent: float
    reduced_at: float
    essential_at: float
    schedule: bool


# --- settings: plugins.entries.usage-governor.settings over plugin.yaml's config_schema defaults ---

def schema_defaults(manifest: Path = MANIFEST) -> dict[str, Any]:
    """`{key: default}` of plugin.yaml's config_schema."""
    import yaml
    data = yaml.safe_load(manifest.read_text()) or {}
    return {k: (v or {}).get("default") for k, v in (data.get("config_schema") or {}).items()}


def configured(plugin_id: str = PLUGIN_ID) -> Mapping[str, Any]:
    """The settings the user set in config.yaml (no defaults), `{}` when none."""
    from hermes_cli.config import load_config_readonly
    raw: Any = load_config_readonly() or {}
    for key in ("plugins", "entries", plugin_id, "settings"):
        raw = raw.get(key) if isinstance(raw, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def _hhmm(key: str, value: Any) -> int:
    if not isinstance(value, str):
        raise SettingsError(f"{key} should be an HH:MM string, not {type(value).__name__}")
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise SettingsError(f"{key} should be HH:MM, not {value!r}")
    h, m = int(parts[0]), int(parts[1])
    if h > 23 or m > 59:
        raise SettingsError(f"{key} should be HH:MM, not {value!r}")
    return h * 60 + m


def _percent(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
        raise SettingsError(f"{key} should be a number above 0 and at most 100, not {value!r}")
    return value


def _merged(values: Mapping, defaults: Mapping) -> dict[str, Any]:
    return {**defaults, **{k: v for k, v in values.items() if v is not None}}


def _schedule(merged: Mapping) -> bool:
    value = merged.get("schedule")
    if not isinstance(value, bool):
        raise SettingsError(f"schedule should be true or false, not {value!r}")
    return value


def read_settings(values: Mapping, defaults: Mapping) -> Settings:
    """Settings from *values* (config.yaml) over *defaults* (config_schema). Raises SettingsError."""
    merged = _merged(values, defaults)
    tz_name = merged.get("timezone")
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise SettingsError(f"timezone should be an IANA zone name, not {tz_name!r}")
    try:
        tz = ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise SettingsError(f"timezone {tz_name!r} is unknown") from exc
    quiet_raw = merged.get("quiet_hours")
    if quiet_raw in (None, []):
        quiet = None
    elif isinstance(quiet_raw, list) and len(quiet_raw) == 2:
        quiet = (_hhmm("quiet_hours[0]", quiet_raw[0]), _hhmm("quiet_hours[1]", quiet_raw[1]))
    else:
        raise SettingsError(f"quiet_hours should be a list of two HH:MM times, or empty, not {quiet_raw!r}")
    review_raw = merged.get("daily_review")
    review = None if review_raw in (None, "") else _hhmm("daily_review", review_raw)
    return Settings(tz=tz, quiet=quiet, review=review,
                    daily_percent=_percent("daily_percent_of_week", merged.get("daily_percent_of_week")),
                    reduced_at=_percent("reduced_at", merged.get("reduced_at")),
                    essential_at=_percent("essential_at", merged.get("essential_at")),
                    schedule=_schedule(merged))


def settings(values: Callable[[], Mapping] = configured,
             defaults: Callable[[], Mapping] = schema_defaults) -> Settings:
    """The settings, read again at each call. Raises SettingsError."""
    try:
        got, base = values(), defaults()
    except Exception as exc:  # noqa: BLE001 - an unreadable config.yaml or plugin.yaml is a settings error
        raise SettingsError(f"settings unreadable: {type(exc).__name__}: {exc}") from exc
    return read_settings(got, base)


def schedule_enabled(values: Callable[[], Mapping] = configured,
                     defaults: Callable[[], Mapping] = schema_defaults) -> bool:
    """`Settings.schedule` from settings(), the user's other settings left at their defaults so a wrong one
    does not stop the job's creation."""
    return settings(lambda: {"schedule": values().get("schedule")}, defaults).schedule


# --- usage source: the only code that knows where the usage comes from ---

def _epoch(iso: Any) -> Optional[float]:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def usage_reading(now: Optional[float] = None, run: Callable[..., Any] = subprocess.run
                  ) -> tuple[Optional[tuple[Any, Any, dict]], str]:
    """`((five_hour_pct, week_pct, resets), "")` from `hermes usage --json`, or `(None, why it is missing)`.

    resets is `{"five_hour": epoch, "seven_day": epoch}` (None when Hermes gives no reset time).
    """
    now = time.time() if now is None else now
    try:
        out = run(list(USAGE_COMMAND), capture_output=True, text=True, timeout=USAGE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f"hermes usage timed out after {USAGE_TIMEOUT_S} s"
    except OSError as exc:
        return None, f"hermes usage did not start: {exc}"
    if out.returncode != 0:
        return None, f"hermes usage exited {out.returncode}: {(out.stderr or '').strip()[:200]}"
    try:
        doc = json.loads(out.stdout)
    except ValueError:
        return None, "hermes usage printed no JSON"
    if not isinstance(doc, dict):
        return None, "hermes usage printed no JSON object"
    if doc.get("unavailable_reason"):
        return None, f"usage unavailable: {str(doc['unavailable_reason'])[:200]}"
    fetched = _epoch(doc.get("fetched_at"))
    if fetched is None:
        return None, "hermes usage gave no fetched_at"
    if now - fetched > STALE_S:
        return None, f"reading is {int(now - fetched)} s old (over {STALE_S} s)"
    windows = {w.get("label"): w for w in doc.get("windows") or [] if isinstance(w, dict)}
    values, resets = [], {}
    for key, label in (("five_hour", SESSION_LABEL), ("seven_day", WEEK_LABEL)):
        window = windows.get(label)
        if window is None:
            return None, f"no window labelled {label!r}"
        pct = window.get("used_percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return None, f"window {label!r} has no used_percent"
        values.append(pct)
        resets[key] = _epoch(window.get("resets_at"))
    return (values[0], values[1], resets), ""


def read_usage(now: Optional[float] = None, run: Callable[..., Any] = subprocess.run
               ) -> Optional[tuple[Any, Any, dict]]:
    """`(five_hour_pct, week_pct, resets)` or None when the reading is missing."""
    return usage_reading(now, run)[0]


# --- rules (the upstream governor.py) ---

def live(pct: Any, reset: Optional[float], now: float) -> Any:
    """The window's % or None when its reset time is past. An unknown reset keeps the %: the thresholds need only
    the %, and `hermes usage` can give `Current session` a % with no reset time (seen: 0 %, `resets_at` null)."""
    return None if pct is None or (reset is not None and reset < now) else pct


def usable_reset(reset: Any, tz: ZoneInfo) -> Optional[float]:
    """*reset*, or None (a missing reset) when it is no date *tz* can hold or day_budget can step a day past."""
    if isinstance(reset, bool) or not isinstance(reset, (int, float)):
        return None
    try:
        datetime.fromtimestamp(reset, tz)
        datetime.fromtimestamp(reset + DAY_S, tz)
    except (OverflowError, ValueError, OSError):
        return None
    return reset


def in_quiet(minute: int, quiet: Optional[tuple[int, int]]) -> bool:
    """Local minute of the day inside [start, end) of the quiet hours, wrapping midnight."""
    if not quiet:
        return False
    start, end = quiet
    return start <= minute < end if start < end else (minute >= start or minute < end)


def day_key(t: datetime, s: Settings) -> str:
    """The working day of *t*: it starts at the minute the quiet hours end (the upstream DAY_START); before that,
    the previous calendar date (not 86,400 s back, which misses a day across a daylight-saving change)."""
    start = s.quiet[1] if s.quiet else 0
    day = t.date() if t.hour * 60 + t.minute >= start else t.date() - timedelta(days=1)
    return day.isoformat()


def day_budget(t: datetime, week: float, week_reset: Optional[float], s: Settings) -> float:
    """At least daily_percent; when earlier days left room, what remains spread over the working days left."""
    if week_reset is None:
        return s.daily_percent
    days, d = 0, t
    while d.timestamp() < week_reset:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)  # same local time on the next calendar day, across daylight-saving changes
    return max(s.daily_percent, round((100 - week) / max(days, 1)))


def day_used(t: datetime, week: float, record: Any, week_reset: Optional[float], s: Settings
             ) -> tuple[float, float, dict]:
    """`(week % used since the day's baseline, day budget, day record)`; new baseline on a new day or week reset."""
    day = day_key(t, s)
    start = record.get("start") if isinstance(record, dict) else None
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        record = {}
    if record.get("day") != day or week < record.get("start", 0):
        record = {"day": day, "start": week, "budget": day_budget(t, week, week_reset, s)}
    return week - record["start"], record.get("budget", s.daily_percent), record


def mode_for(t: datetime, five: Any, week: Any, used_today: Optional[float], budget: float, s: Settings) -> str:
    """The upstream rules, most restrictive first."""
    minute = t.hour * 60 + t.minute
    top = max([p for p in (five, week) if p is not None], default=0)
    if in_quiet(minute, s.quiet):
        return "quiet"
    if used_today is not None and used_today >= budget:
        return "budget"
    if top >= s.essential_at or (used_today is not None and used_today >= budget * 0.8):
        return "essential"
    if top >= s.reduced_at or (used_today is not None and used_today >= budget * 0.6):
        return "reduced"
    return "full"


def _clock(t: datetime, now: datetime) -> str:
    """`HH:MM` when *t* falls on *now*'s date, else `Ddd HH:MM`."""
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%a %H:%M")


def _next_at(t: datetime, minute: int) -> datetime:
    """The next local time of day *minute* after *t*."""
    at = t.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    return at if at > t else at + timedelta(days=1)


def _local(epoch: float, tz: Any) -> Optional[datetime]:
    """*epoch* as a local datetime, or None when it is out of range."""
    try:
        return datetime.fromtimestamp(epoch, tz)
    except (OverflowError, OSError, ValueError):
        return None


def until_for(t: datetime, mode: str, override_until: Any, five: Any, week: Any, resets: Mapping,
              used_today: Optional[float], budget: float, s: Settings) -> Optional[datetime]:
    """When *mode* is expected to end, local, or None (full).

    Active override: its until (None when out of range). quiet: the end of the quiet hours. budget: the
    next day's start. essential/reduced: the LATEST end among what holds the mode (each window at or above
    the threshold ends at its reset; the day budget ends at the next day's start), since the mode drops
    only when all of them have; a holding window with no usable reset time: None (no time is known);
    nothing holds: the next day's start.
    """
    if isinstance(override_until, (int, float)) and not isinstance(override_until, bool):
        return _local(override_until, s.tz)
    day_start = _next_at(t, s.quiet[1] if s.quiet else 0)
    if mode == "quiet":
        return _next_at(t, s.quiet[1]) if s.quiet else None
    if mode == "budget":
        return day_start
    if mode not in ("essential", "reduced"):
        return None
    threshold, share = (s.essential_at, 0.8) if mode == "essential" else (s.reduced_at, 0.6)
    ends = []
    for key, pct in (("five_hour", five), ("seven_day", week)):
        reset = resets.get(key)
        end = _local(reset, s.tz) if isinstance(reset, (int, float)) and reset > t.timestamp() else None
        if pct is not None and pct >= threshold:
            if end is None:
                return None  # this window holds the mode and its end is unknown: any time quoted could be too early
            ends.append(end)
    if used_today is not None and used_today >= budget * share:
        ends.append(day_start)
    return max(ends) if ends else day_start


# --- files ---

def data_dir() -> Path:
    """`plugin-data/usage-governor` of the default profile, whatever profile runs this."""
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir("default") / "plugin-data" / PLUGIN_ID


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_atomic(path: Path, text: str) -> None:
    """Write *text* to a temporary file next to *path*, then replace *path* with it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, indent=2) + "\n")


# --- one tick ---

def _pct(value: Any) -> str:
    """A % for the review line: `40%`, `12.5%`, `--` when unknown."""
    return "--" if value is None else f"{value:g}%"


def tick(now: Optional[float] = None, data: Optional[Path] = None,
         read_settings_fn: Callable[[], Settings] = settings,
         reading: Callable[[float], tuple[Optional[tuple[Any, Any, dict]], str]] = usage_reading,
         out: Any = None) -> dict:
    """One reading: decide the mode, write state.json, one journal line, the heartbeat; print the review line.

    An active override.json replaces only the mode: the day's usage is still counted (day.json, state `day`).
    """
    now = time.time() if now is None else now
    out = sys.stdout if out is None else out
    d = Path(data) if data is not None else data_dir()
    d.mkdir(parents=True, exist_ok=True)
    last = _load_json(d / STATE)
    last = last if isinstance(last, dict) else {}
    try:
        s = read_settings_fn()
    except SettingsError as exc:
        # A wrong setting: record it and do nothing else (no mode change, journal or heartbeat).
        state = {**last, "settings_error": str(exc)}
        _write_json(d / STATE, state)
        return state
    t = datetime.fromtimestamp(now, s.tz)
    got, why = reading(now)
    five = week = week_reset = None
    resets: dict[str, str] = {}
    raw_resets: dict[str, Optional[float]] = {}
    if got is not None:
        five_raw, week_raw, raw_resets = got
        # A reset no date can hold (e.g. 1e20) would overflow or never end day_budget's loop: read it as missing.
        raw_resets = {k: usable_reset(v, s.tz) for k, v in raw_resets.items()}
        five = live(five_raw, raw_resets.get("five_hour"), now)
        week = live(week_raw, raw_resets.get("seven_day"), now)
        week_reset = raw_resets.get("seven_day")
        resets = {k: at.strftime("%a %H:%M") for k, v in raw_resets.items() if v and (at := _local(v, s.tz))}
    fallback = five is None and week is None
    if fallback:
        # No usable reading (missing, stale, or every window past its reset): keep the last week value and
        # drop the 5-hour window (the upstream rule).
        kept = last.get("seven_day")
        week = kept if isinstance(kept, (int, float)) and not isinstance(kept, bool) else None
        if got is not None:
            why = "every usage window is past its reset"
    ov = _load_json(d / OVERRIDE)
    try:
        override_on = now < ov.get("until", 0)
    except Exception:  # noqa: BLE001 - a malformed override file is no override (upstream)
        override_on = False
    ignored = None
    if override_on and ov.get("mode", "full") not in MODES:
        # An unknown mode is no override; the state says why.
        override_on = False
        ignored = f"override.json mode {ov.get('mode')!r} is not one of {', '.join(MODES)}"
    # The day's accounting runs on every tick, override or not, so an override never hides spending from
    # the day budget or the carry-over: the override only replaces the mode.
    record = _load_json(d / DAY)
    used = None
    budget = s.daily_percent
    if week is not None:
        used, budget, fresh = day_used(t, week, record, week_reset, s)
        if fresh is not record:
            _write_json(d / DAY, fresh)
            record = fresh
    if override_on:
        mode = ov.get("mode", "full")
        until_ok = _local(ov.get("until"), s.tz) is not None
        override = {"active": True, "until": ov.get("until") if until_ok else None, "ignored": None}
    else:
        override = {"active": False, "until": None, "ignored": ignored}
        mode = mode_for(t, five, week, used, budget, s)
    record = record if isinstance(record, dict) else {}
    until = until_for(t, mode, ov.get("until") if override_on else None, five, week, raw_resets, used, budget, s)
    prev = last.get("mode")
    state = {
        "mode": mode,
        "since": last.get("since") if mode == prev and last.get("since") is not None else int(now),
        "five_hour": five,
        "seven_day": week,
        "resets": resets,
        "reading_at": last.get("reading_at") if fallback else int(now),
        "source_ok": not fallback,
        "reading": "missing" if fallback else "ok",
        "reason": why or None,
        "override": override,
        "quiet": in_quiet(t.hour * 60 + t.minute, s.quiet),
        "until": None if until is None else int(until.timestamp()),
        "until_local": None if until is None else _clock(until, t),
        "day": {"day": record.get("day"), "start": record.get("start"), "budget": record.get("budget"),
                "used": used},
        "reviewed": last.get("reviewed"),
        "settings_error": None,
    }
    today = t.strftime("%Y-%m-%d")
    line = None
    if (s.review is not None and t.weekday() < 5 and t.hour * 60 + t.minute >= s.review
            and state["reviewed"] != today and mode != "quiet"):
        state["reviewed"] = today
        line = f"REVIEW daily usage · 5h {_pct(five)} · 7d {_pct(week)} · log {d / JOURNAL}"
    _write_json(d / STATE, state)
    with open(d / JOURNAL, "a") as f:
        f.write(json.dumps({"at": int(now), "five_hour": five, "seven_day": week, "mode": mode,
                            "source_ok": not fallback}) + "\n")
    beat = d / HEARTBEAT
    write_atomic(beat, f"{int(now)}\n")
    os.utime(beat, (now, now))
    if line is not None:
        print(line, file=out, flush=True)
    return state


# --- health ---

def health(now: Optional[float] = None, data: Optional[Path] = None) -> tuple[bool, list[str]]:
    """`(ok, reasons)`: ok when the heartbeat is younger than 10 min and state.json is readable."""
    now = time.time() if now is None else now
    d = Path(data) if data is not None else data_dir()
    reasons = []
    try:
        age = now - (d / HEARTBEAT).stat().st_mtime
    except OSError:
        reasons.append("no heartbeat: the tick has not run")
    else:
        if age >= HEARTBEAT_MAX_AGE_S:
            reasons.append(f"last heartbeat {int(age // 60)} min ago")
    try:
        json.loads((d / STATE).read_text())["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        reasons.append("state.json unreadable")
    return not reasons, reasons
