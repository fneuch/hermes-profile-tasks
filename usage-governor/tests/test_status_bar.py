"""The desktop status bar: the read-only GET /state route (dashboard/plugin_api.py) and the item's rendered text
(desktop/plugin.js through tests/status_render.mjs).

The route runs on a fake HOME + HERMES_HOME under /tmp with Hermes' real profile code: a tree under ~/.hermes
would resolve to the real root (hermes_constants._hermes_root_of).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

PLUGIN = Path(__file__).resolve().parent.parent
RENDER = PLUGIN / "tests" / "status_render.mjs"
TZ = ZoneInfo("America/Toronto")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state(mode="full", five=12.0, week=40.0, override=None, **extra) -> dict:
    return {"mode": mode, "since": 1795600000, "five_hour": five, "seven_day": week,
            "resets": {"five_hour": "Wed 12:30", "seven_day": "Sun 05:00"}, "reading_at": 1795600000,
            "source_ok": True, "reading": "ok", "reason": None,
            "override": override or {"active": False, "until": None, "ignored": None},
            "day": {"day": "2026-11-25", "start": 30.0, "budget": 20, "used": 10.0}, "reviewed": None,
            "settings_error": None, **extra}


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("hermes_cli"),
                     "fastapi or hermes_cli not installed (run with the Hermes venv python)")
class StateRouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="usage-governor-route-")
        self.addCleanup(tmp.cleanup)
        self.fake = Path(tmp.name)
        self.root = self.fake / ".hermes"
        self.root.mkdir()
        self.data = self.root / "plugin-data" / "usage-governor"
        env = {"HOME": str(self.fake), "HERMES_HOME": str(self.root), "HERMES_DATA_DIR_SUFFIX": ""}
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = _load("usage_governor_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/usage-governor")
        self.client = TestClient(app, raise_server_exceptions=False)

    def get(self) -> dict:
        r = self.client.get("/api/plugins/usage-governor/state")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def write(self, value, beat_age: float = 0) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "state.json").write_text(value if isinstance(value, str) else json.dumps(value))
        beat = self.data / "heartbeat"
        beat.write_text("1\n")
        t = time.time() - beat_age
        os.utime(beat, (t, t))

    def snapshot(self) -> list:
        return sorted((str(p.relative_to(self.fake)), p.stat().st_mtime_ns) for p in self.fake.rglob("*"))

    def test_state_present_is_returned_with_health_ok_and_nothing_written(self):
        self.write(state())
        self.assertEqual(self.api.governor().data_dir(), self.data)
        before = self.snapshot()
        self.assertEqual(self.get(), {"state": state(), "health": {"ok": True, "reasons": []}})
        self.assertEqual(self.snapshot(), before)

    def test_missing_state_is_null_not_500(self):
        self.assertEqual(self.get(), {"state": None, "health": {"ok": False, "reasons": [
            "no heartbeat: the tick has not run", "state.json unreadable"]}})
        self.assertEqual(list(self.fake.rglob("*")), [self.root])

    def test_unreadable_state_is_null(self):
        self.write("{broken")
        self.assertEqual(self.get(), {"state": None, "health": {"ok": False, "reasons": ["state.json unreadable"]}})
        (self.data / "state.json").unlink()
        (self.data / "state.json").mkdir()
        self.assertEqual(self.get()["state"], None)

    def test_health_bad_keeps_the_state(self):
        self.write(state(), beat_age=900)
        self.assertEqual(self.get(), {"state": state(), "health": {"ok": False, "reasons": [
            "last heartbeat 15 min ago"]}})

    def test_governor_failure_is_a_reason_not_500(self):
        def boom():
            raise RuntimeError("no profiles")
        with mock.patch.object(self.api.governor(), "data_dir", boom):
            body = self.get()
        self.assertEqual(body, {"state": None, "health": {"ok": False, "reasons": [
            "governor unavailable: RuntimeError: no profiles"]}})


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StatusItemRenderTest(unittest.TestCase):
    def render(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="usage-governor-render-") as tmp:
            f = Path(tmp) / "payload.json"
            f.write_text(json.dumps(payload))
            env = {**os.environ, "TZ": "America/Toronto", "TMPDIR": tmp}
            out = subprocess.run(["node", str(RENDER), str(f)], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_full_12_40(self):
        got = self.render({"body": {"state": state(), "health": {"ok": True, "reasons": []}}})
        self.assertEqual(got["register"], {"id": "status", "area": "right", "order": 100})
        self.assertEqual(got["query"]["refetchInterval"], 30000)
        self.assertEqual(got["text"], "full · 5h 12% · wk 40%")
        self.assertEqual((got["warn"], got["icon"]), (False, []))
        self.assertEqual(got["tip"], "Usage governor: full\n5-hour window: 12%, resets Wed 12:30\nWeek: 40%, resets Sun 05:00")

    def test_essential_80_96_with_override(self):
        until = int(datetime(2026, 11, 25, 14, 0, tzinfo=TZ).timestamp())
        s = state("essential", 80.0, 96.0, override={"active": True, "until": until, "ignored": None})
        got = self.render({"body": {"state": s, "health": {"ok": True, "reasons": []}}})
        self.assertEqual(got["text"], "essential · 5h 80% · wk 96%")
        self.assertFalse(got["warn"])
        self.assertIn("Override active until Wed 14:00", got["tip"].split("\n"))

    def test_missing_5h(self):
        s = state("reduced", None, 55.0, reading="missing", source_ok=False, reason="hermes usage exited 1: x")
        got = self.render({"body": {"state": s, "health": {"ok": True, "reasons": []}}})
        self.assertEqual(got["text"], "reduced · 5h -- · wk 55%")
        self.assertIn("Reading: hermes usage exited 1: x", got["tip"].split("\n"))

    def test_missing_reading_warns(self):
        s = state("reduced", None, 55.0, reading="missing", source_ok=False, reason="hermes usage exited 1: x",
                  resets={})
        got = self.render({"body": {"state": s, "health": {"ok": True, "reasons": []}}})
        self.assertEqual((got["text"], got["warn"], got["icon"]), ("reduced · 5h -- · wk 55%", True, ["warning"]))
        self.assertIn("Reading: hermes usage exited 1: x", got["tip"].split("\n"))

    def test_settings_error_warns(self):
        s = state(settings_error="timezone 'Mars/Base' is unknown")
        got = self.render({"body": {"state": s, "health": {"ok": True, "reasons": []}}})
        self.assertEqual((got["warn"], got["icon"]), (True, ["warning"]))
        self.assertIn("Settings: timezone 'Mars/Base' is unknown", got["tip"].split("\n"))

    def test_health_bad(self):
        got = self.render({"body": {"state": state(), "health": {"ok": False, "reasons": ["last heartbeat 15 min ago"]}}})
        self.assertEqual(got["text"], "full · 5h 12% · wk 40%")
        self.assertEqual((got["warn"], got["icon"]), (True, ["warning"]))
        self.assertIn("Health: last heartbeat 15 min ago", got["tip"].split("\n"))

    def test_no_state_and_backend_error_warn(self):
        got = self.render({"body": {"state": None, "health": {"ok": False, "reasons": ["state.json unreadable"]}}})
        self.assertEqual((got["text"], got["warn"], got["icon"]), ("-- · 5h -- · wk --", True, ["warning"]))
        self.assertIn("Health: state.json unreadable", got["tip"].split("\n"))
        got = self.render({"error": "502 Bad Gateway"})
        self.assertEqual((got["text"], got["warn"]), ("-- · 5h -- · wk --", True))
        self.assertIn("backend unreachable: 502 Bad Gateway", got["tip"].split("\n"))
        self.assertNotIn("Health: backend unreachable: 502 Bad Gateway", got["tip"])
        got = self.render({"body": {"state": None}})
        self.assertEqual(got["tip"].split("\n")[-1], "no state yet")


if __name__ == "__main__":
    unittest.main()
