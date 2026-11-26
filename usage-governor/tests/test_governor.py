"""usage-governor unit tests: the rules (the upstream values), the usage source, the tick's files, health, register."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

PLUGIN = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hermes-usage.json"


def _load(name: str, path: Path, package: bool = False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = _load("usage_governor_plugin", PLUGIN / "__init__.py", package=True)
g = plugin.governor
TZ = ZoneInfo("America/Toronto")
DEFAULTS = g.schema_defaults()
S = g.read_settings({}, DEFAULTS)


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, TZ).isoformat()


# Wednesday 2026-11-25, local; the week resets Sunday 05:00 local.
NOW = at("2026-11-25T10:00")
WEEK_RESET = at("2026-11-29T05:00")
FIVE_RESET = at("2026-11-25T12:30")


def doc(five=30.0, week=40.0, fetched=NOW, five_reset=FIVE_RESET, week_reset=WEEK_RESET, **extra):
    base = json.loads(FIXTURE.read_text())
    base["fetched_at"] = iso(fetched)
    base["windows"][0].update(used_percent=five, resets_at=five_reset and iso(five_reset))
    base["windows"][1].update(used_percent=week, resets_at=week_reset and iso(week_reset))
    base.update(extra)
    return base


def runner(stdout="", code=0, exc=None):
    def run(cmd, **kw):
        assert cmd == ["hermes", "usage", "--json"] and kw["timeout"] == 15
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="boom")
    return run


def reading_of(document):
    return lambda now: g.usage_reading(now, runner(json.dumps(document)))


def missing(now):
    return None, "hermes usage exited 1: boom"


class TmpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def tick(self, now=NOW, reading=None, settings=S, out=None):
        return g.tick(now=now, data=self.tmp, read_settings_fn=lambda: settings,
                      reading=reading or reading_of(doc()), out=out if out is not None else io.StringIO())

    def journal(self):
        return [json.loads(x) for x in (self.tmp / "journal.jsonl").read_text().splitlines()]


class SettingsTest(unittest.TestCase):
    def test_defaults_are_upstream_values(self):
        self.assertEqual(DEFAULTS, {"timezone": "America/Toronto", "quiet_hours": ["02:50", "08:00"],
                                    "daily_review": "16:55", "daily_percent_of_week": 20, "reduced_at": 50,
                                    "essential_at": 75, "schedule": True, "enforce": True,
                                    "reduced_options": ["Fast", "Balance"]})
        self.assertEqual((S.quiet, S.review, S.tz), ((170, 480), 16 * 60 + 55, TZ))

    def test_config_overrides_defaults_and_none_keeps_default(self):
        s = g.read_settings({"reduced_at": 40, "essential_at": None, "quiet_hours": []}, DEFAULTS)
        self.assertEqual((s.reduced_at, s.essential_at, s.quiet), (40, 75, None))

    def test_wrong_setting_is_named(self):
        cases = [({"timezone": "Mars/Base"}, "timezone"), ({"quiet_hours": ["25:00", "08:00"]}, "quiet_hours[0]"),
                 ({"quiet_hours": ["02:50"]}, "quiet_hours"), ({"daily_review": "late"}, "daily_review"),
                 ({"reduced_at": 0}, "reduced_at"), ({"essential_at": True}, "essential_at"),
                 ({"daily_percent_of_week": 120}, "daily_percent_of_week"), ({"schedule": "yes"}, "schedule")]
        for values, name in cases:
            with self.subTest(values=values):
                with self.assertRaises(g.SettingsError) as err:
                    g.read_settings(values, DEFAULTS)
                self.assertTrue(str(err.exception).startswith(name), err.exception)
                self.assertIsInstance(err.exception, ValueError)

    def test_unreadable_settings_are_a_settings_error(self):
        def boom():
            raise OSError("gone")
        with self.assertRaisesRegex(g.SettingsError, "settings unreadable: OSError"):
            g.settings(values=boom)

    def test_schedule_alone_ignores_other_wrong_settings(self):
        self.assertIs(g.schedule_enabled(values=lambda: {"timezone": "Mars/Base"}), True)
        self.assertIs(g.schedule_enabled(values=lambda: {"schedule": False}), False)


class SourceTest(unittest.TestCase):
    def test_real_hermes_output_shape(self):
        real = json.loads(FIXTURE.read_text())
        fetched = datetime.fromisoformat(real["fetched_at"]).timestamp()
        got = g.read_usage(fetched + 1, runner(json.dumps(real)))
        self.assertEqual(got[:2], (real["windows"][0]["used_percent"], real["windows"][1]["used_percent"]))
        self.assertEqual(got[2]["seven_day"], datetime.fromisoformat(real["windows"][1]["resets_at"]).timestamp())

    def test_labels_pick_the_windows_whatever_their_order(self):
        d = doc(five=12.5, week=61)
        d["windows"].reverse()
        self.assertEqual(g.read_usage(NOW, runner(json.dumps(d))),
                         (12.5, 61, {"five_hour": FIVE_RESET, "seven_day": WEEK_RESET}))

    def test_missing_reading_says_why(self):
        no_label = doc()
        no_label["windows"] = [w for w in no_label["windows"] if w["label"] != "Current week"]
        cases = [(runner(code=1), "exited 1"),
                 (runner(exc=subprocess.TimeoutExpired("hermes", 15)), "timed out after 15 s"),
                 (runner(exc=FileNotFoundError("hermes")), "did not start"),
                 (runner(stdout="not json"), "no JSON"),
                 (runner(stdout="[]"), "no JSON object"),
                 (runner(stdout=json.dumps(doc(unavailable_reason="signed out"))), "unavailable: signed out"),
                 (runner(stdout=json.dumps(no_label)), "'Current week'"),
                 (runner(stdout=json.dumps(doc(five=None))), "no used_percent"),
                 (runner(stdout=json.dumps(doc(fetched_at=None))), "no fetched_at"),
                 (runner(stdout=json.dumps(doc(fetched=NOW - 901))), "901 s old")]
        for run, text in cases:
            with self.subTest(text=text):
                got, why = g.usage_reading(NOW, run)
                self.assertIsNone(got)
                self.assertIsNone(g.read_usage(NOW, run))
                self.assertIn(text, why)

    def test_reading_900_s_old_still_counts(self):
        self.assertIsNotNone(g.read_usage(NOW, runner(json.dumps(doc(fetched=NOW - 900)))))


class RulesTest(unittest.TestCase):
    def mode(self, five=0, week=0, used=0, budget=20, when="2026-11-25T10:00"):
        return g.mode_for(local(when), five, week, used, budget, S)

    def test_window_thresholds_each_window_alone(self):
        for which in ("five", "week"):
            for pct, mode in ((49, "full"), (50, "reduced"), (74, "reduced"), (75, "essential")):
                with self.subTest(which=which, pct=pct):
                    self.assertEqual(self.mode(**{which: pct}), mode)

    def test_window_past_its_reset_is_skipped(self):
        self.assertIsNone(g.live(80, NOW - 1, NOW))
        self.assertEqual(g.live(80, None, NOW), 80)  # no reset time: the % still counts
        self.assertEqual(g.live(80, NOW + 1, NOW), 80)
        self.assertEqual(self.mode(five=g.live(80, NOW - 1, NOW), week=10), "full")

    def test_quiet_hours_edges(self):
        for hhmm, mode in (("02:49", "full"), ("02:50", "quiet"), ("07:59", "quiet"), ("08:00", "full")):
            with self.subTest(hhmm=hhmm):
                self.assertEqual(self.mode(when=f"2026-11-25T{hhmm}"), mode)

    def test_quiet_hours_wrapping_midnight(self):
        night = (22 * 60, 6 * 60)
        self.assertTrue(g.in_quiet(23 * 60, night) and g.in_quiet(60, night))
        self.assertFalse(g.in_quiet(12 * 60, night) or g.in_quiet(6 * 60, night))

    def test_day_usage_thresholds(self):
        for used, mode in ((11, "full"), (12, "reduced"), (15, "reduced"), (16, "essential"), (19, "essential"),
                           (20, "budget"), (25, "budget")):
            with self.subTest(used=used):
                self.assertEqual(self.mode(five=10, week=10, used=used), mode)

    def test_budget_wins_over_window_rules_and_nothing_known_is_full(self):
        self.assertEqual(self.mode(five=99, week=99, used=20), "budget")
        self.assertEqual(self.mode(five=None, week=None, used=None), "full")

    def test_day_starts_when_quiet_hours_end(self):
        self.assertEqual(g.day_key(local("2026-11-25T07:59"), S), "2026-11-24")
        self.assertEqual(g.day_key(local("2026-11-25T08:00"), S), "2026-11-25")

    def test_day_starts_at_the_minute_quiet_hours_end(self):
        late = g.read_settings({"quiet_hours": ["02:50", "08:30"]}, DEFAULTS)
        self.assertEqual(g.day_key(local("2026-05-05T08:15"), late), "2026-05-04")
        self.assertEqual(g.day_key(local("2026-05-05T08:30"), late), "2026-05-05")

    def test_day_is_the_previous_calendar_date_across_daylight_saving(self):
        # DST starts 2026-03-08 02:00 and ends 2026-11-01 02:00 in Toronto: those days last 23 h and 25 h.
        for when, day in (("2026-03-09T00:30", "2026-03-08"), ("2026-03-09T07:59", "2026-03-08"),
                          ("2026-03-08T07:59", "2026-03-07"), ("2026-11-01T07:59", "2026-10-31"),
                          ("2026-11-02T00:30", "2026-11-01"), ("2026-11-02T07:59", "2026-11-01")):
            with self.subTest(when=when):
                self.assertEqual(g.day_key(local(when), S), day)

    def test_day_budget_carry_over(self):
        # Thursday, week 40: Thu and Fri left before Sunday's reset -> max(20, round(60/2)).
        self.assertEqual(g.day_budget(local("2026-11-26T10:00"), 40, WEEK_RESET, S), 30)
        self.assertEqual(g.day_budget(local("2026-11-25T10:00"), 70, WEEK_RESET, S), 20)
        self.assertEqual(g.day_budget(local("2026-11-25T10:00"), 10, None, S), 20)

    def test_day_budget_steps_calendar_days_across_daylight_saving(self):
        # Friday 09:00 EST, reset Tuesday 09:30 EDT (DST starts Sunday 2026-03-08): Fri, Mon and Tue are left,
        # so max(20, round(90/3)). Stepping 86,400 s lands on Tuesday 10:00, past the reset, and drops Tuesday.
        self.assertEqual(g.day_budget(local("2026-03-06T09:00"), 10, at("2026-03-10T09:30"), S), 30)

    def test_baseline_reset_on_new_day_and_on_week_reset(self):
        t = local("2026-11-25T10:00")
        used, _, rec = g.day_used(t, 40, {"day": "2026-11-24", "start": 10, "budget": 25}, WEEK_RESET, S)
        self.assertEqual((used, rec["start"], rec["day"]), (0, 40, "2026-11-25"))
        used, budget, rec = g.day_used(t, 45, rec, WEEK_RESET, S)
        self.assertEqual((used, budget), (5, 20))
        used, _, rec = g.day_used(t, 3, rec, WEEK_RESET, S)
        self.assertEqual((used, rec["start"]), (0, 3))

    def test_day_record_without_start_is_reset(self):
        t = local("2026-11-25T10:00")
        for bad in ({"day": "2026-11-25", "budget": 25}, {"day": "2026-11-25", "start": "x", "budget": 25},
                    {"day": "2026-11-25", "start": True, "budget": 25}, None):
            with self.subTest(record=bad):
                used, budget, rec = g.day_used(t, 40, bad, WEEK_RESET, S)
                self.assertEqual((used, budget, rec), (0, 20, {"day": "2026-11-25", "start": 40, "budget": 20}))


class ReturnTimeTest(TmpCase):
    """The tick writes quiet, until and until_local (the hook only reads them)."""

    def fields(self, state):
        return state["mode"], state["quiet"], state["until"], state["until_local"]

    def test_full_has_no_return_time(self):
        self.assertEqual(self.fields(self.tick()), ("full", False, None, None))

    def test_override_until(self):
        (self.tmp / "override.json").write_text(json.dumps({"mode": "reduced", "until": NOW + 3600}))
        self.assertEqual(self.fields(self.tick()), ("reduced", False, int(NOW + 3600), "11:00"))

    def test_out_of_range_override_until_is_unknown(self):
        for until in (1e20, float("inf")):
            with self.subTest(until=until):
                (self.tmp / "override.json").write_text(json.dumps({"mode": "quiet", "until": until}))
                self.assertEqual(self.fields(self.tick())[::2], ("quiet", None))
                self.assertEqual(json.loads((self.tmp / "state.json").read_text())["until_local"], None)
                self.assertEqual(self.tick(now=NOW + 60)["mode"], "quiet")

    def test_out_of_range_override_until_is_null_in_strict_json(self):
        def refuse(name):
            raise ValueError(f"not strict JSON: {name}")
        for until in (1e20, float("inf")):
            with self.subTest(until=until):
                (self.tmp / "override.json").write_text(json.dumps({"mode": "quiet", "until": until}))
                self.tick()
                state = json.loads((self.tmp / "state.json").read_text(), parse_constant=refuse)
                self.assertEqual((state["override"]["active"], state["override"]["until"]), (True, None))

    def test_quiet_ends_with_quiet_hours(self):
        self.assertEqual(self.fields(self.tick(now=at("2026-11-25T03:00"))),
                         ("quiet", True, int(at("2026-11-25T08:00")), "08:00"))

    def test_budget_is_next_day_start(self):
        self.tick(reading=reading_of(doc(five=10, week=40)))  # day budget 20
        got = self.tick(now=NOW + 60, reading=reading_of(doc(five=10, week=60, fetched=NOW + 60)))
        self.assertEqual(self.fields(got), ("budget", False, int(at("2026-11-26T08:00")), "Thu 08:00"))

    def test_essential_two_windows_latest_reset(self):
        got = self.tick(reading=reading_of(doc(five=80, week=90)))
        self.assertEqual(self.fields(got), ("essential", False, int(WEEK_RESET), "Sun 05:00"))
        # The later reset wins whichever window it belongs to.
        week_first = at("2026-11-25T11:00")
        got = self.tick(reading=reading_of(doc(five=80, week=90, week_reset=week_first)))
        self.assertEqual(self.fields(got), ("essential", False, int(FIVE_RESET), "12:30"))

    def test_holding_window_without_reset_quotes_no_time(self):
        # Thu 14:00: the 5-hour window (95, no reset time) and the week (92, resets 14:30) hold essential;
        # the mode drops only when both have reset, so 14:30 would be too early and no time is known.
        thu = at("2026-11-26T14:00")
        got = self.tick(now=thu, reading=reading_of(doc(five=95, week=92, fetched=thu, five_reset=None,
                                                         week_reset=at("2026-11-26T14:30"))))
        self.assertEqual(self.fields(got), ("essential", False, None, None))

    def test_essential_without_window_is_next_day(self):
        # Day share only (16 >= 0.8 x 20); the week (56) is under essential_at.
        self.tick(reading=reading_of(doc(five=10, week=40)))
        got = self.tick(now=NOW + 60, reading=reading_of(doc(five=10, week=56, fetched=NOW + 60)))
        self.assertEqual(self.fields(got), ("essential", False, int(at("2026-11-26T08:00")), "Thu 08:00"))

    def test_reduced_window_reset(self):
        got = self.tick(reading=reading_of(doc(five=55, week=10)))
        self.assertEqual(self.fields(got), ("reduced", False, int(FIVE_RESET), "12:30"))

    def test_reduced_window_and_day_share_takes_the_later(self):
        # reduced_at 60: the 5-hour window (65, resets 12:30) and the day share (12 >= 0.6 x 20) both hold.
        s = g.read_settings({"reduced_at": 60}, DEFAULTS)
        self.tick(reading=reading_of(doc(five=10, week=40)), settings=s)
        got = self.tick(now=NOW + 60, reading=reading_of(doc(five=65, week=52, fetched=NOW + 60)), settings=s)
        self.assertEqual(self.fields(got), ("reduced", False, int(at("2026-11-26T08:00")), "Thu 08:00"))

    def test_return_time_uses_the_tick_timezone(self):
        paris = g.read_settings({"timezone": "Europe/Paris", "quiet_hours": []}, DEFAULTS)
        got = self.tick(reading=reading_of(doc(five=55, week=10)), settings=paris)
        self.assertEqual(self.fields(got), ("reduced", False, int(FIVE_RESET), "18:30"))


class TickTest(TmpCase):
    def test_writes_state_journal_day_heartbeat_and_prints_nothing(self):
        out = io.StringIO()
        state = self.tick(out=out)
        self.assertEqual(json.loads((self.tmp / "state.json").read_text()), state)
        self.assertEqual({k: state[k] for k in ("mode", "since", "five_hour", "seven_day", "reading_at", "source_ok",
                                                "reading", "reason", "override", "day", "resets", "reviewed")},
                         {"mode": "full", "since": int(NOW), "five_hour": 30.0, "seven_day": 40.0,
                          "reading_at": int(NOW), "source_ok": True, "reading": "ok", "reason": None,
                          "override": {"active": False, "until": None, "ignored": None},
                          "day": {"day": "2026-11-25", "start": 40.0, "budget": 20, "used": 0},
                          "resets": {"five_hour": "Wed 12:30", "seven_day": "Sun 05:00"}, "reviewed": None})
        self.assertEqual(self.journal(), [{"at": int(NOW), "five_hour": 30.0, "seven_day": 40.0, "mode": "full",
                                           "source_ok": True}])
        self.assertEqual((self.tmp / "heartbeat").stat().st_mtime, NOW)
        self.assertEqual(json.loads((self.tmp / "day.json").read_text())["start"], 40.0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(list(self.tmp.glob(".*.tmp")), [])

    def test_one_journal_line_per_tick_and_since_kept_until_mode_changes(self):
        self.tick()
        self.assertEqual(self.tick(now=NOW + 60)["since"], int(NOW))
        changed = self.tick(now=NOW + 120, reading=reading_of(doc(five=80, fetched=NOW + 120)))
        self.assertEqual((changed["mode"], changed["since"]), ("essential", int(NOW + 120)))
        self.assertEqual(len(self.journal()), 3)

    def test_out_of_range_reset_is_shown_as_missing(self):
        reading = lambda now: ((30.0, 40.0, {"five_hour": 1e20, "seven_day": WEEK_RESET}), "")  # noqa: E731
        state = self.tick(reading=reading)
        self.assertEqual(json.loads((self.tmp / "state.json").read_text()), state)
        self.assertEqual(state["resets"], {"seven_day": "Sun 05:00"})

    def test_day_usage_drives_budget_mode(self):
        self.tick()
        self.assertEqual(self.tick(now=NOW + 60, reading=reading_of(doc(week=60, fetched=NOW + 60)))["mode"],
                         "budget")

    def test_unusable_week_reset_is_a_missing_reset(self):
        # A reset no date can hold (1e20, inf, NaN) or the last ISO date (the day loop would step past year 9999)
        # gives the same tick as a missing reset instead of an endless loop or an overflow.
        def fixed(reset):
            return lambda now: ((30.0, 40.0, {"five_hour": FIVE_RESET, "seven_day": reset}), "")
        expected = self.tick(reading=fixed(None))
        last_iso = datetime.fromisoformat("9999-12-31T23:59:59+00:00").timestamp()
        for reset in (1e20, float("inf"), float("nan"), last_iso):
            with self.subTest(reset=reset):
                for name in ("state.json", "day.json"):
                    (self.tmp / name).unlink(missing_ok=True)
                self.assertEqual(self.tick(reading=fixed(reset)), expected)
                self.assertEqual(json.loads((self.tmp / "day.json").read_text())["budget"], S.daily_percent)

    def test_window_without_reset_time_keeps_its_percent(self):
        # Real `hermes usage --json` (2026-11-26): `Current session` 0.0 with resets_at null (no 5-hour window
        # running), `Current week` 100.0. The thresholds need only the %; the reset time stays unknown.
        state = self.tick(reading=reading_of(doc(five=0.0, five_reset=None, week=100.0)))
        self.assertEqual((state["five_hour"], state["seven_day"], state["mode"], state["source_ok"]),
                         (0.0, 100.0, "essential", True))
        self.assertNotIn("five_hour", state["resets"])
        # Only the week holds the mode: its reset is the return time, though the 5-hour window has none.
        self.assertEqual((state["until"], state["until_local"]), (int(WEEK_RESET), "Sun 05:00"))
        self.assertEqual(self.journal()[-1]["five_hour"], 0.0)
        state = self.tick(now=NOW + 60, reading=reading_of(doc(five=80.0, five_reset=None, week=10.0,
                                                                  fetched=NOW + 60)))
        self.assertEqual((state["five_hour"], state["mode"]), (80.0, "essential"))

    def test_reset_before_the_first_local_date_is_a_missing_reset(self):
        # The zero time 0001-01-01 UTC is year 0 in a zone west of UTC: formatting it would raise and skip the tick.
        first_iso = datetime.fromisoformat("0001-01-01T00:00:00+00:00").timestamp()
        resets = {"five_hour": FIVE_RESET, "seven_day": None}
        for window in ("five_hour", "seven_day"):
            def fixed(reset, window=window):
                return lambda now: ((30.0, 40.0, {**resets, window: reset}), "")
            for name in ("state.json", "day.json"):
                (self.tmp / name).unlink(missing_ok=True)
            expected = self.tick(reading=fixed(None))
            with self.subTest(window=window, reset=first_iso):
                for name in ("state.json", "day.json"):
                    (self.tmp / name).unlink(missing_ok=True)
                self.assertEqual(self.tick(reading=fixed(first_iso)), expected)
                written = json.loads((self.tmp / "state.json").read_text())
                self.assertNotIn(window, written["resets"])


class FallbackTest(TmpCase):
    def check_kept(self, reading, reason):
        self.tick(reading=reading_of(doc(five=10, week=80)))
        state = self.tick(now=NOW + 60, reading=reading)
        self.assertEqual((state["seven_day"], state["five_hour"], state["mode"]), (80, None, "essential"))
        self.assertEqual((state["source_ok"], state["reading"], state["reading_at"]), (False, "missing", int(NOW)))
        self.assertIn(reason, state["reason"])
        self.assertEqual(self.journal()[-1], {"at": int(NOW + 60), "five_hour": None, "seven_day": 80,
                                              "mode": "essential", "source_ok": False})

    def test_missing_reading_keeps_last_week_and_drops_five_hour(self):
        self.check_kept(missing, "exited 1")

    def test_stale_reading_keeps_last_week_and_drops_five_hour(self):
        stale = lambda now: g.usage_reading(now, runner(json.dumps(doc(five=0, week=0, fetched=now - 901))))  # noqa: E731
        self.check_kept(stale, "901 s old")

    def test_missing_first_reading_is_full_with_reason(self):
        state = self.tick(reading=missing)
        self.assertEqual((state["mode"], state["seven_day"], state["source_ok"]), ("full", None, False))
        self.assertIsNone(state["reading_at"])

    def test_missing_reading_loosens_when_five_hour_drove_the_mode(self):
        self.assertEqual(self.tick(reading=reading_of(doc(five=80, week=30)))["mode"], "essential")
        state = self.tick(now=NOW + 60, reading=missing)
        self.assertEqual((state["mode"], state["five_hour"], state["seven_day"]), ("full", None, 30))

    def test_both_windows_expired_is_a_missing_reading(self):
        expired = reading_of(doc(five=0, week=0, fetched=NOW + 60, five_reset=NOW, week_reset=NOW))
        self.check_kept(expired, "every usage window is past its reset")

    def test_one_window_expired_is_still_a_reading(self):
        cases = [("five_hour", {"five_reset": NOW}, (None, 40, "full")),
                 ("seven_day", {"week_reset": NOW}, (90, None, "essential"))]
        for which, reset, expect in cases:
            with self.subTest(expired=which):
                (self.tmp / "state.json").unlink(missing_ok=True)
                self.tick(reading=reading_of(doc(five=10, week=80)))
                state = self.tick(now=NOW + 60, reading=reading_of(doc(five=90, week=40, fetched=NOW + 60, **reset)))
                self.assertEqual((state["five_hour"], state["seven_day"], state["mode"]), expect)
                self.assertEqual((state["source_ok"], state["reading"], state["reading_at"], state["reason"]),
                                 (True, "ok", int(NOW + 60), None))
                self.assertIs(self.journal()[-1]["source_ok"], True)


class OverrideTest(TmpCase):
    def write(self, value):
        (self.tmp / "override.json").write_text(value if isinstance(value, str) else json.dumps(value))

    def test_active_override_wins_then_expires(self):
        self.write({"mode": "full", "until": NOW + 3600})
        state = self.tick(reading=reading_of(doc(five=90)))
        self.assertEqual((state["mode"], state["override"]),
                         ("full", {"active": True, "until": NOW + 3600, "ignored": None}))
        later = self.tick(now=NOW + 3600, reading=reading_of(doc(five=90, fetched=NOW + 3600)))
        self.assertEqual((later["mode"], later["override"]["active"]), ("essential", False))

    def test_day_usage_still_counted_during_an_override(self):
        # The override covers the day's first reading: the day still starts at 40, so 40 then 60 is 20 used
        # (the whole 20% day budget) while the override sets the mode; once it ends the budget mode applies.
        self.write({"mode": "full", "until": NOW + 3600})
        self.tick()
        state = self.tick(now=NOW + 60, reading=reading_of(doc(week=60, fetched=NOW + 60)))
        self.assertEqual((state["mode"], state["override"]["active"]), ("full", True))
        self.assertEqual(state["day"], {"day": "2026-11-25", "start": 40.0, "budget": 20, "used": 20.0})
        self.assertEqual(json.loads((self.tmp / "day.json").read_text())["start"], 40.0)
        later = self.tick(now=NOW + 3600, reading=reading_of(doc(week=60, fetched=NOW + 3600)))
        self.assertEqual((later["mode"], later["override"]["active"], later["day"]["used"]), ("budget", False, 20.0))

    def test_override_without_mode_is_full_and_malformed_is_ignored(self):
        self.write({"until": NOW + 60})
        self.assertEqual(self.tick(reading=reading_of(doc(five=90)))["mode"], "full")
        for bad in ({"mode": "full", "until": "soon"}, "{not json", {"mode": "full", "until": NOW - 1}):
            with self.subTest(bad=bad):
                self.write(bad)
                self.assertEqual(self.tick(reading=reading_of(doc(five=90)))["mode"], "essential")

    def test_unknown_mode_is_no_override_and_state_says_why(self):
        for bad in ("turbo", None, 3):
            with self.subTest(mode=bad):
                self.write({"mode": bad, "until": NOW + 3600})
                state = self.tick(reading=reading_of(doc(five=90)))
                self.assertEqual(state["mode"], "essential")
                self.assertEqual(state["override"], {
                    "active": False, "until": None,
                    "ignored": f"override.json mode {bad!r} is not one of quiet, budget, essential, reduced, full"})
        self.write({"mode": "reduced", "until": NOW + 3600})
        self.assertEqual(self.tick(reading=reading_of(doc(five=90)))["override"]["ignored"], None)


class ReviewTest(TmpCase):
    REVIEW = at("2026-11-25T16:55")

    def at_review(self, now, settings=S):
        out = io.StringIO()
        reading = reading_of(doc(fetched=now, five_reset=now + 3600, week_reset=now + 86400))
        return self.tick(now=now, reading=reading, settings=settings, out=out), out.getvalue()

    def test_printed_once_at_review_time_on_weekday(self):
        self.assertEqual(self.at_review(self.REVIEW - 60)[1], "")
        state, printed = self.at_review(self.REVIEW)
        self.assertEqual(printed, f"REVIEW daily usage · 5h 30% · 7d 40% · log {self.tmp / 'journal.jsonl'}\n")
        self.assertEqual(state["reviewed"], "2026-11-25")
        self.assertEqual(self.at_review(self.REVIEW + 60)[1], "")
        self.assertEqual(self.at_review(self.REVIEW + 86400)[1].count("REVIEW"), 1)

    def test_first_tick_after_review_time_prints_once(self):
        # No tick at 16:55 (gateway down): the 17:10 tick prints it, the next one does not.
        late = at("2026-11-25T17:10")
        state, printed = self.at_review(late)
        self.assertEqual((printed.count("REVIEW"), state["reviewed"]), (1, "2026-11-25"))
        self.assertEqual(self.at_review(late + 60)[1], "")

    def test_percent_format(self):
        self.assertEqual([g._pct(v) for v in (40.0, 12.5, 0, 100, None)], ["40%", "12.5%", "0%", "100%", "--"])
        reading = reading_of(doc(five=12.5, fetched=self.REVIEW, five_reset=self.REVIEW + 3600,
                                 week_reset=self.REVIEW - 1))
        out = io.StringIO()
        self.tick(now=self.REVIEW, reading=reading, out=out)
        self.assertTrue(out.getvalue().startswith("REVIEW daily usage · 5h 12.5% · 7d -- · log "), out.getvalue())

    def test_not_on_weekend(self):
        self.assertEqual(self.at_review(at("2026-11-28T17:00"))[1], "")

    def test_not_in_quiet_mode(self):
        evening_quiet = g.read_settings({"quiet_hours": ["16:00", "18:00"]}, DEFAULTS)
        state, printed = self.at_review(self.REVIEW, evening_quiet)
        self.assertEqual((state["mode"], printed, state["reviewed"]), ("quiet", "", None))


class SettingsErrorTickTest(TmpCase):
    def test_error_recorded_and_nothing_else(self):
        self.tick()
        beat = (self.tmp / "heartbeat").stat().st_mtime

        def bad():
            raise g.SettingsError("timezone 'Mars/Base' is unknown")
        state = g.tick(now=NOW + 60, data=self.tmp, read_settings_fn=bad,
                       reading=reading_of(doc(five=90)), out=io.StringIO())
        self.assertEqual((state["settings_error"], state["mode"], state["since"]),
                         ("timezone 'Mars/Base' is unknown", "full", int(NOW)))
        self.assertEqual(len(self.journal()), 1)
        self.assertEqual((self.tmp / "heartbeat").stat().st_mtime, beat)
        self.assertIsNone(self.tick(now=NOW + 120)["settings_error"])


class HealthTest(TmpCase):
    def test_fresh_old_and_unreadable(self):
        self.assertEqual(g.health(now=NOW, data=self.tmp),
                         (False, ["no heartbeat: the tick has not run", "state.json unreadable"]))
        self.tick()
        self.assertEqual(g.health(now=NOW + 599, data=self.tmp), (True, []))
        self.assertEqual(g.health(now=NOW + 600, data=self.tmp), (False, ["last heartbeat 10 min ago"]))
        (self.tmp / "state.json").write_text("{broken")
        self.assertEqual(g.health(now=NOW + 1, data=self.tmp), (False, ["state.json unreadable"]))

    def test_tick_entry_health_exit_codes(self):
        tick = _load("usage_governor_tick", PLUGIN / "tick.py")
        clock = SimpleNamespace(time=lambda: NOW + 1)
        with mock.patch.object(tick.governor, "data_dir", lambda: self.tmp), \
                mock.patch.object(tick.governor, "time", clock):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(tick.main(["--health"]), 1)
            self.assertTrue(out.getvalue().startswith("usage-governor: not ok: no heartbeat"))
            self.tick(now=NOW)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(tick.main(["--health"]), 0)
            self.assertEqual(out.getvalue(), "usage-governor: ok\n")


class FakeCron:
    def __init__(self, jobs=(), fail=None):
        self.jobs, self.calls, self.fail = list(jobs), [], fail

    def __call__(self, **kw):
        self.calls.append(kw)
        if kw["action"] == self.fail:
            return {"success": False, "error": "nope"}
        if kw["action"] == "list":
            return {"success": True, "jobs": [dict(job) for job in self.jobs]}
        if kw["action"] == "update":
            # Hermes lists a job's schedule as its display string; a cron expression displays as itself.
            next(job for job in self.jobs if job.get("job_id") == kw["job_id"])["schedule"] = kw["schedule"]
            return {"success": True}
        self.jobs.append({"job_id": "abc", "name": kw["name"], "schedule": kw["schedule"]})
        return {"success": True, "job_id": "abc"}


CREATE = {"action": "create", "name": "usage-governor", "schedule": "* * * * *", "no_agent": True,
          "script": "usage-governor-tick.py", "deliver": "local", "failure_deliver": "local"}
LIST = {"action": "list", "include_disabled": True}


class RegisterTest(TmpCase):
    """register() against a fake HERMES_HOME tree, the cron API stubbed."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "hermes-home"
        self.cron = FakeCron()
        self.default = True
        self.schedule = True
        for target, value in ((plugin, {"check_internals": lambda ctx=None: [], "cron_api": self.cron,
                                        "check_hook_internals": lambda ctx=None: [], "register_hook": lambda ctx: True,
                                        "_is_default_profile": lambda: self.default,
                                        "_hermes_home": lambda: self.home}),
                              (g, {"data_dir": lambda: self.home / "plugin-data" / "usage-governor",
                                   "schedule_enabled": lambda: self.schedule})):
            for name, fn in value.items():
                patcher = mock.patch.object(target, name, fn)
                patcher.start()
                self.addCleanup(patcher.stop)

    def wrapper(self):
        return self.home / "scripts" / "usage-governor-tick.py"

    def test_default_profile_creates_wrapper_and_job_once(self):
        plugin.register(None)
        plugin.register(None)
        self.assertEqual(self.cron.calls, [{"action": "list", "include_disabled": True}, CREATE,
                                           {"action": "list", "include_disabled": True}])
        self.assertIn(str(plugin.PLUGIN_DIR), self.wrapper().read_text())
        self.assertTrue(self.wrapper().stat().st_mode & 0o111)

    def test_paused_job_is_kept(self):
        self.cron.jobs.append({"name": "usage-governor", "enabled": False})
        plugin.register(None)
        self.assertNotIn(CREATE, self.cron.calls)

    def test_job_created_with_every_1m_moves_to_every_minute_once(self):
        # An install from before the fix has `every 1m`, which runs every 2 minutes (see ScheduleCadenceTest).
        self.cron.jobs.append({"job_id": "2643d14b6ba6", "name": "usage-governor", "schedule": "every 1m",
                               "enabled": False})
        plugin.register(None)
        plugin.register(None)
        self.assertEqual(self.cron.calls, [LIST, {"action": "update", "job_id": "2643d14b6ba6",
                                                  "schedule": "* * * * *"}, LIST])
        self.assertEqual(self.cron.jobs, [{"job_id": "2643d14b6ba6", "name": "usage-governor",
                                           "schedule": "* * * * *", "enabled": False}])

    def test_job_with_a_schedule_set_by_hand_is_kept(self):
        self.cron.jobs.append({"job_id": "j1", "name": "usage-governor", "schedule": "every 5m"})
        plugin.register(None)
        self.assertEqual(self.cron.calls, [LIST])

    def test_other_profile_does_nothing(self):
        self.default = False
        with mock.patch.object(plugin, "check_internals", lambda ctx=None: ["tools.cronjob_tools.cronjob"]):
            with self.assertNoLogs(plugin.logger):
                plugin.register(None)
        self.assertEqual((self.cron.calls, self.home.exists()), ([], False))

    def test_schedule_false_does_nothing(self):
        self.schedule = False
        plugin.register(None)
        self.assertEqual((self.cron.calls, self.home.exists()), ([], False))

    def test_missing_internals_stop_before_anything(self):
        with mock.patch.object(plugin, "check_internals", lambda ctx=None: ["tools.cronjob_tools.cronjob"]):
            with self.assertLogs(plugin.logger, "ERROR") as logs:
                plugin.register(None)
        self.assertIn("internals missing", logs.output[0])
        self.assertEqual(self.cron.calls, [])

    def test_profile_check_raising_installs_nothing(self):
        def broken():
            raise TypeError("profile_matches_home() takes 0 arguments")
        with mock.patch.object(plugin, "_is_default_profile", broken):
            with self.assertLogs(plugin.logger, "ERROR") as logs:
                plugin.register(None)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("profile check failed", logs.output[0])
        self.assertEqual((self.cron.calls, self.wrapper().exists()), ([], False))

    def test_cron_failure_is_logged_not_raised(self):
        self.cron.fail = "create"
        with self.assertLogs(plugin.logger, "ERROR") as logs:
            plugin.register(None)
        self.assertIn("installing the cron job failed", logs.output[0])

    def test_wrapper_runs_tick_and_is_silent_when_plugin_gone(self):
        gone = plugin.write_wrapper(self.tmp / "a", self.tmp / "missing")
        run = subprocess.run([sys.executable, str(gone)], capture_output=True, text=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        fake = self.tmp / "plugin"
        fake.mkdir()
        (fake / "tick.py").write_text("import sys; print('ran', sys.argv[1:])\n")
        wrapper = plugin.write_wrapper(self.tmp / "b", fake)
        run = subprocess.run([sys.executable, str(wrapper), "--health"], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.stdout, "ran ['--health']\n")


class ScheduleCadenceTest(unittest.TestCase):
    """JOB_SCHEDULE through Hermes' own schedule code: due again at the next ticker wake (one tick a minute)."""

    def test_next_run_after_the_run_ends_is_due_at_the_next_wake(self):
        try:
            from cron.jobs import TICKER_INTERVAL_SECONDS, compute_next_run, parse_schedule
        except Exception as exc:  # noqa: BLE001 - only Hermes' Python with hermes-agent on the path has it
            self.skipTest(f"needs Hermes' cron (cron.jobs), see README Tests: {exc!r}")
        schedule = parse_schedule(plugin.JOB_SCHEDULE)
        wake = datetime(2026, 11, 26, 16, 0, 35, tzinfo=TZ)
        # Hermes stamps last_run_at when the script ends and computes next_run_at from it (cron/jobs.py
        # mark_job_run -> _advance_after_run -> compute_next_run); a tick takes a few seconds.
        for took in (1, 5, 20):
            with self.subTest(took=took):
                end = wake + timedelta(seconds=took)
                nxt = datetime.fromisoformat(compute_next_run(schedule, end.isoformat(), timezone="America/Toronto"))
                self.assertLessEqual(nxt, wake + timedelta(seconds=TICKER_INTERVAL_SECONDS))


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "needs Hermes' Python (hermes_cli), see README Tests")
class RealTreeRegisterTest(unittest.TestCase):
    """register() on a fake HOME + HERMES_HOME under /tmp, Hermes' real profile and path code; only cron stubbed.

    The tree must sit outside ~/.hermes: a HERMES_HOME under it resolves to the real root
    (hermes_constants._hermes_root_of), so TMPDIR (a profile's cache) is not used.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="usage-governor-test-")
        self.addCleanup(tmp.cleanup)
        self.fake = Path(tmp.name)
        self.root = self.fake / ".hermes"
        self.cron = FakeCron()
        patcher = mock.patch.object(plugin, "cron_api", self.cron)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, home: Path):
        env = {"HOME": str(self.fake), "HERMES_HOME": str(home), "HERMES_DATA_DIR_SUFFIX": ""}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(plugin.check_internals(None), [])
            plugin.register(None)

    def test_default_profile_real_paths_create_wrapper_and_job_once(self):
        self.root.mkdir()
        self.load(self.root)
        self.load(self.root)
        wrapper = self.root / "scripts" / "usage-governor-tick.py"
        self.assertIn(repr(str(plugin.PLUGIN_DIR)), wrapper.read_text())
        self.assertEqual([c for c in self.cron.calls if c["action"] == "create"], [CREATE])
        self.assertEqual(len(self.cron.calls), 3)
        self.assertTrue((self.root / "plugin-data" / "usage-governor" / "install.lock").is_file())

    def test_other_profile_real_paths_do_nothing(self):
        profile = self.root / "profiles" / "coder"
        profile.mkdir(parents=True)
        self.load(profile)
        self.assertEqual(self.cron.calls, [])
        self.assertEqual(sorted(p.relative_to(self.fake) for p in self.fake.rglob("*")),
                         [Path(".hermes"), Path(".hermes/profiles"), Path(".hermes/profiles/coder")])


if __name__ == "__main__":
    unittest.main()
