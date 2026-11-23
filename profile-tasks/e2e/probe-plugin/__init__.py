"""e2e only: a plain Python caller inside the gateway process, with no conversation (as a plugin backend is).

It runs each request file the trial drops in ``$HERMES_HOME/pt-probe/`` against ``profile_tasks.send``/``result``
and writes the response next to it.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

MODULE = "hermes_plugins.profile_tasks"


def _serve(box: Path) -> None:
    box.mkdir(parents=True, exist_ok=True)
    while True:
        for request in sorted(box.glob("request-*.json")):
            response = box / request.name.replace("request-", "response-")
            try:
                call = json.loads(request.read_text())
                mod = sys.modules[MODULE]
                fn = {"send": mod.send, "result": mod.result}[call["op"]]
                out = {"ok": True, "value": fn(**call["args"])}
            except Exception as exc:  # noqa: BLE001 - every failure goes back to the trial
                out = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "trace": traceback.format_exc()}
            out["modules"] = sorted(m for m in sys.modules if m.startswith(MODULE))
            request.unlink()
            # Renamed into place: the trial reads the response as soon as its path exists.
            partial = response.with_suffix(".part")
            partial.write_text(json.dumps(out, default=str))
            partial.replace(response)
        time.sleep(0.2)


def register(ctx) -> None:
    from hermes_constants import get_hermes_home
    threading.Thread(target=_serve, args=(get_hermes_home() / "pt-probe",), name="pt-probe", daemon=True).start()
