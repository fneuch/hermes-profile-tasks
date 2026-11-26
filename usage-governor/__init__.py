"""usage-governor: its pre_tool_call hook in every profile; under the default profile, its every-minute cron job.

register() registers enforce.check as a pre_tool_call hook in any profile. Under the default profile it also writes ~/.hermes/scripts/usage-governor-tick.py (a thin wrapper running this plugin's tick.py) and
creates the no-model cron job `usage-governor` through Hermes' own cron API when no job of that name exists.
Under any other profile, or with the `schedule` setting false, it installs no job.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import enforce, governor

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
JOB_NAME = "usage-governor"
JOB_SCHEDULE = "every 1m"
WRAPPER_NAME = "usage-governor-tick.py"
HOOK = "pre_tool_call"
# The cronjob() parameters the job creation and lookup pass.
CRON_PARAMS = ("action", "name", "schedule", "no_agent", "script", "deliver", "failure_deliver", "include_disabled")
WRAPPER = '''#!/usr/bin/env python3
"""Written by the usage-governor plugin at each load: runs its tick.py; exits silently when it is gone."""
import os
import runpy
import sys

TICK = os.path.join({plugin_dir!r}, "tick.py")
if not os.path.isfile(TICK):
    sys.exit(0)
sys.argv = [TICK] + sys.argv[1:]
runpy.run_path(TICK, run_name="__main__")
'''


def check_internals(ctx: Any = None) -> list[str]:
    """Names of the Hermes internals this plugin uses that the running Hermes no longer has."""
    missing: list[str] = []

    def need(module: str, names: tuple[str, ...]) -> Any:
        try:
            mod = __import__(module, fromlist=list(names))
        except Exception as exc:  # noqa: BLE001 - any import failure means the internal is gone
            missing.append(f"{module} ({exc.__class__.__name__}: {exc})")
            return None
        for name in names:
            if not hasattr(mod, name):
                missing.append(f"{module}.{name}")
        return mod

    need("hermes_cli.profiles", ("get_profile_dir", "profile_matches_home"))
    need("hermes_cli.config", ("load_config_readonly",))
    need("hermes_constants", ("get_hermes_home",))
    tools = need("tools.cronjob_tools", ("cronjob",))
    cronjob = getattr(tools, "cronjob", None)
    if cronjob is not None:
        params = inspect.signature(cronjob).parameters
        missing += [f"tools.cronjob_tools.cronjob({p}=)" for p in CRON_PARAMS if p not in params]
    return missing


def check_hook_internals(ctx: Any = None) -> list[str]:
    """The Hermes internals the pre_tool_call hook needs that the running Hermes no longer has."""
    missing: list[str] = []
    if ctx is not None and not callable(getattr(ctx, "register_hook", None)):
        missing.append("PluginContext.register_hook")
    try:
        from hermes_cli import plugins
    except Exception as exc:  # noqa: BLE001 - any import failure means the internal is gone
        missing.append(f"hermes_cli.plugins ({exc.__class__.__name__}: {exc})")
    else:
        if HOOK not in getattr(plugins, "VALID_HOOKS", ()):
            missing.append(f"hermes_cli.plugins.VALID_HOOKS[{HOOK}]")
    return missing


def cron_api(**kwargs: Any) -> dict:
    """Hermes' cron API, the one `hermes cron create` / `hermes cron list` call."""
    from tools.cronjob_tools import cronjob
    return json.loads(cronjob(**kwargs))


def wrapper_text(plugin_dir: Path) -> str:
    return WRAPPER.format(plugin_dir=str(plugin_dir))


def write_wrapper(scripts: Path, plugin_dir: Path) -> Path:
    """Write or refresh the wrapper in *scripts*; untouched when already current."""
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / WRAPPER_NAME
    text = wrapper_text(plugin_dir)
    with contextlib.suppress(OSError):
        if path.read_text() == text:
            return path
    governor.write_atomic(path, text)
    path.chmod(0o755)
    return path


@contextlib.contextmanager
def _locked(path: Path):
    # One loader at a time between the job lookup and its creation (several processes load plugins at once).
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            import fcntl
        except ImportError:
            yield
            return
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def ensure_job(api: Callable[..., dict]) -> str:
    """Create the cron job unless a job named usage-governor exists (paused included): `created` or `exists`."""
    listed = api(action="list", include_disabled=True)
    if not listed.get("success"):
        raise RuntimeError(f"cron list failed: {listed.get('error')}")
    if any(job.get("name") == JOB_NAME for job in listed.get("jobs") or []):
        return "exists"
    created = api(action="create", name=JOB_NAME, schedule=JOB_SCHEDULE, no_agent=True, script=WRAPPER_NAME,
                  deliver="local", failure_deliver="local")
    if not created.get("success"):
        raise RuntimeError(f"cron create failed: {created.get('error')}")
    return "created"


def install(home: Path, data: Path, plugin_dir: Path = PLUGIN_DIR, api: Callable[..., dict] = cron_api) -> str:
    """Wrapper in `<home>/scripts`, then the job: `created` or `exists`."""
    write_wrapper(Path(home) / "scripts", plugin_dir)
    with _locked(Path(data) / "install.lock"):
        return ensure_job(api)


def _is_default_profile() -> bool:
    from hermes_cli.profiles import profile_matches_home
    return profile_matches_home("default")


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def register_hook(ctx: Any) -> bool:
    """Register the enforcement hook on *ctx*; False when *ctx* cannot take hooks."""
    add = getattr(ctx, "register_hook", None)
    if not callable(add):
        return False
    try:
        add(HOOK, enforce.check)
    except Exception:  # noqa: BLE001 - a refused hook must not break the plugin load
        logger.exception("usage-governor: registering the %s hook failed", HOOK)
        return False
    return True


def register(ctx: Any) -> None:
    # The hook's internals are checked on their own: a gap there never stops the tick install.
    hook_missing = check_hook_internals(ctx)
    if hook_missing:
        logger.error("usage-governor: Hermes internals for the %s hook missing after an update: %s",
                     HOOK, "; ".join(hook_missing))
    if not register_hook(ctx):
        logger.error("usage-governor: the %s hook is not registered; nothing is enforced in this profile", HOOK)
    try:
        default = _is_default_profile()
    except Exception:  # noqa: BLE001 - a failing profile check must never install under another profile
        logger.exception("usage-governor: the profile check failed; the cron job is not installed (internals: %s)",
                         "; ".join(check_internals(ctx)) or "all present")
        return
    if not default:
        return
    missing = check_internals(ctx)
    if missing:
        logger.error("usage-governor: Hermes internals missing after an update; the cron job is not installed: %s",
                     "; ".join(missing))
        return
    try:
        if not governor.schedule_enabled():
            return
    except governor.SettingsError as exc:
        logger.error("usage-governor: the cron job is not installed: %s", exc)
        return
    try:
        result = install(_hermes_home(), governor.data_dir(), PLUGIN_DIR, cron_api)
    except Exception:  # noqa: BLE001 - a failed install must not break the plugin load
        logger.exception("usage-governor: installing the cron job failed")
        return
    if result == "created":
        logger.info("usage-governor: created the cron job %s (%s)", JOB_NAME, JOB_SCHEDULE)
    else:
        logger.debug("usage-governor: the cron job %s already exists", JOB_NAME)
