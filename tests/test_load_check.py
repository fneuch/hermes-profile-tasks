"""Load-time check of the profile-tasks plugin: present internals pass, a removed one blocks the tool.

Run: ``tests/run_unit.sh`` (isolated HOME/HERMES_HOME, the installed Hermes interpreter).
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

    def __init__(self):
        self.tools = []

    def register_tool(self, **kw):
        self.tools.append(kw)

    def get_config(self, key, default=None):
        return default


def main() -> int:
    failures = 0
    mod = load()
    missing = mod.check_internals()
    print("present internals -> missing:", missing)
    failures += bool(missing)

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
