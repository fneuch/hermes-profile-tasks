"""Load-time check of the profile-tasks plugin: present internals pass, a removed one blocks the tool.

Also: a plugin context without ``_manager`` or ``on_unload`` blocks the tool.

Run: ``PT_SCRIPT=tests/test_load_check.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "profile-tasks" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("profile_tasks_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    profile_name = "default"
    state = None
    _manager = None

    def __init__(self):
        self.tools = []
        self.hooks = []

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def on_unload(self, callback):
        pass

    def get_config(self, key, default=None):
        return default


def main() -> int:
    failures = 0
    mod = load()
    missing = mod.check_internals()
    print("present internals -> missing:", missing)
    failures += bool(missing)

    from hermes_cli.plugins_context import PluginContext
    missing = mod.check_internals(PluginContext(None, object()))
    print("real PluginContext -> missing:", missing)
    failures += bool(missing)

    class NoManagerCtx(FakeCtx):
        def __getattribute__(self, name):
            if name == "_manager":
                raise AttributeError(name)
            return super().__getattribute__(name)

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger().addHandler(handler)
    ctx = NoManagerCtx()
    mod.register(ctx)
    logging.getLogger().removeHandler(handler)
    logged = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
    reply = json.loads(ctx.tools[0]["handler"]({"action": "status"}))
    print("context without _manager -> logged:", logged)
    print("context without _manager -> tool reply:", reply)
    name = "hermes_cli.plugins_context.PluginContext._manager"
    failures += not (any(name in m for m in logged) and name in json.dumps(reply) and "error" in reply)

    class NoUnloadCtx(FakeCtx):
        def __getattribute__(self, name):
            if name == "on_unload":
                raise AttributeError(name)
            return super().__getattribute__(name)

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger().addHandler(handler)
    ctx = NoUnloadCtx()
    try:
        mod.register(ctx)
    except Exception as exc:  # noqa: BLE001 - a raise here is the half-load this case guards against
        print("context without on_unload -> register raised:", repr(exc))
    logging.getLogger().removeHandler(handler)
    logged = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
    reply = json.loads(ctx.tools[0]["handler"]({"action": "status"})) if ctx.tools else None
    print("context without on_unload -> logged:", logged)
    print("context without on_unload -> tool reply:", reply)
    name = "hermes_cli.plugins_context.PluginContext.on_unload"
    failures += not (reply is not None and any(name in m for m in logged) and name in json.dumps(reply)
                     and "error" in reply and ctx.hooks)

    import hermes_cli.gateway_client as gc
    saved = gc._session_ticket
    del gc._session_ticket
    try:
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        ctx = FakeCtx()
        mod.register(ctx)
        logged = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
        print("removed _session_ticket -> logged:", logged)
        reply = json.loads(ctx.tools[0]["handler"]({"action": "status"}))
        print("removed _session_ticket -> tool reply:", reply)
        ok = any("hermes_cli.gateway_client._session_ticket" in m for m in logged) and \
            "hermes_cli.gateway_client._session_ticket" in json.dumps(reply) and "error" in reply
        failures += not ok
    finally:
        gc._session_ticket = saved
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
