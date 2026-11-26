"""usage-governor enforcement hook tests: starts, options, fail-open cases, messages, register, tool_call path."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

PLUGIN = Path(__file__).resolve().parent.parent


def _plugin():
    # Same module name as test_governor.py so both files share one plugin instance.
    if "usage_governor_plugin" in sys.modules:
        return sys.modules["usage_governor_plugin"]
    spec = importlib.util.spec_from_file_location(
        "usage_governor_plugin", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["usage_governor_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = _plugin()
g = plugin.governor
e = plugin.enforce
TZ = ZoneInfo("America/Toronto")
DEFAULTS = g.schema_defaults()
S = g.read_settings({}, DEFAULTS)
RULES = e.read_enforcement({}, DEFAULTS)
HAS_HERMES = importlib.util.find_spec("hermes_cli") is not None


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


# Wednesday 2026-11-25 10:00 local.
NOW = at("2026-11-25T10:00")
START = {"action": "start", "profile": "analyst", "goal": "x"}
OTHER_ACTIONS = ("follow_up", "correct", "status", "approval")
MODES = ("quiet", "budget", "essential", "reduced", "full")


def state(mode="full", **extra):
    base = {"mode": mode, "five_hour": 10, "seven_day": 10, "resets": {"five_hour": "Wed 12:30",
            "seven_day": "Sun 05:00"}, "override": {"active": False, "until": None, "ignored": None}}
    base.update(extra)
    return base


ENFORCE_FALSE = "plugins:\n  entries:\n    usage-governor:\n      settings:\n        enforce: false\n"


def tasks(*options):
    return {"tasks": [{"goal": "g"} if o is None else {"goal": "g", "option": o} for o in options]}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        e._logged.clear()

    def put(self, value):
        (self.tmp / "state.json").write_text(value if isinstance(value, str) else json.dumps(value))

    def decide(self, tool, args, mode=None, rules=RULES, now=NOW, st=None):
        if st is not None or mode is not None:
            self.put(st if st is not None else state(mode))
        return e.decide(tool, args, now=now, data=self.tmp, read_enforcement=lambda: rules)

    def blocked(self, *a, **kw):
        got = self.decide(*a, **kw)
        return got is not None and got["action"] == "block"


class StartTest(Case):
    def test_start_blocked_in_quiet_budget_essential_only(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(self.blocked("profile_task", START, mode), mode in ("quiet", "budget", "essential"))

    def test_other_actions_never_blocked(self):
        for mode in MODES:
            for action in OTHER_ACTIONS:
                with self.subTest(mode=mode, action=action):
                    self.assertFalse(self.blocked("profile_task", {"action": action, "task_id": "t"}, mode))

    def test_other_tools_untouched(self):
        self.assertIsNone(self.decide("terminal", {"command": "ls"}, "quiet"))


class OptionTest(Case):
    def test_delegate_options_in_reduced(self):
        cases = [(("Deep",), True), (("Fast",), False), (("Balance",), False), ((None,), True),
                 (("Fast", "Deep"), True), (("Fast", None), True), (("Fast", "Balance"), False)]
        for options, expect in cases:
            with self.subTest(options=options):
                self.assertEqual(self.blocked("delegate_task", tasks(*options), "reduced"), expect)

    def test_action_case_is_ignored(self):
        for action in ("Spawn", " SPAWN "):
            with self.subTest(action=action):
                self.assertTrue(self.blocked("delegate_task", {"action": action, **tasks("Deep")}, "reduced"))
        self.assertFalse(self.blocked("delegate_task", {"action": "List"}, "quiet"))

    def test_tasks_as_json_string(self):
        fast = json.dumps(tasks("Fast")["tasks"])
        deep = json.dumps(tasks("Deep")["tasks"])
        self.assertFalse(self.blocked("delegate_task", {"tasks": fast}, "reduced"))
        self.assertTrue(self.blocked("delegate_task", {"tasks": deep}, "reduced"))

    def test_tool_call_arguments_as_json_string(self):
        wrapped = {"calls": [{"name": "profile_task", "arguments": START}]}
        self.assertTrue(self.blocked("tool_call", wrapped, "quiet"))  # control: dict arguments
        as_string = {"calls": [{"name": "profile_task", "arguments": json.dumps(START)}]}
        self.assertTrue(self.blocked("tool_call", as_string, "quiet"))
        self.assertFalse(self.blocked("tool_call", {"calls": [{"name": "profile_task", "arguments": "{bad"}]},
                                      "quiet"))

    def test_unparsable_tasks_string_is_unnamed(self):
        msg = self.decide("delegate_task", {"tasks": "[{not json"}, "reduced")["message"]
        self.assertIn("without an option is not allowed in reduced mode", msg)

    def test_legacy_goal_form_blocked(self):
        self.assertTrue(self.blocked("delegate_task", {"goal": "g", "context": "c"}, "reduced"))

    def test_stricter_modes_inherit_and_full_allows(self):
        for mode in ("quiet", "budget", "essential"):
            with self.subTest(mode=mode):
                self.assertTrue(self.blocked("delegate_task", tasks("Deep"), mode))
                self.assertFalse(self.blocked("delegate_task", tasks("Fast"), mode))
        self.assertFalse(self.blocked("delegate_task", tasks("Deep"), "full"))
        self.assertFalse(self.blocked("delegate_task", {"goal": "g"}, "full"))

    def test_control_actions_not_checked(self):
        for action in ("list", "steer", "stop", "continue"):
            with self.subTest(action=action):
                self.assertFalse(self.blocked("delegate_task", {"action": action, "subagent_id": "x"}, "quiet"))
        self.assertTrue(self.blocked("delegate_task", {"action": "spawn", **tasks("Deep")}, "reduced"))

    def test_profile_task_option(self):
        self.assertTrue(self.blocked("profile_task", {"action": "follow_up", "option": "Deep"}, "reduced"))
        self.assertTrue(self.blocked("profile_task", {"action": "follow_up", "option": ""}, "reduced"))
        self.assertFalse(self.blocked("profile_task", {"action": "follow_up", "option": "Fast"}, "reduced"))
        # No option key: not option-checked; a start in reduced passes.
        self.assertFalse(self.blocked("profile_task", START, "reduced"))

    def test_reduced_options_setting(self):
        rules = e.read_enforcement({"reduced_options": ["Fast"]}, DEFAULTS)
        self.assertTrue(self.blocked("delegate_task", tasks("Balance"), "reduced", rules=rules))


class FailOpenTest(Case):
    def test_state_missing_or_unreadable(self):
        with self.assertLogs(e.logger, "ERROR") as logs:
            self.assertIsNone(self.decide("profile_task", START))
            for bad in ("{broken", "[]", json.dumps({"mode": "turbo"})):
                with self.subTest(bad=bad):
                    self.put(bad)
                    self.assertIsNone(self.decide("profile_task", START))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("no usable state", logs.output[0])
        # Control: the same call with a valid state blocks.
        self.assertTrue(self.blocked("profile_task", START, "quiet"))

    def test_stale_state_still_applies(self):
        self.put(state("quiet"))
        (self.tmp / "heartbeat").write_text(f"{int(NOW - 3600)}\n")
        self.assertTrue(self.blocked("profile_task", START, now=NOW + 86400))

    def test_exception_blocks_nothing_and_logs_once(self):
        with mock.patch.object(e, "decide", side_effect=RuntimeError("boom")):
            with self.assertLogs(e.logger, "ERROR") as logs:
                self.assertIsNone(e.check("profile_task", START))
                self.assertIsNone(e.check("profile_task", START))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("boom", logs.output[0])

    def test_enforce_false(self):
        rules = e.read_enforcement({"enforce": False}, DEFAULTS)
        self.assertIsNone(self.decide("profile_task", START, "quiet", rules=rules))
        # Control: same fixture with enforce true blocks.
        self.assertTrue(self.blocked("profile_task", START, "quiet", rules=e.read_enforcement({"enforce": True}, DEFAULTS)))

    def test_bad_settings_block_nothing_and_log_once(self):
        for values in ({"reduced_options": "Fast"}, {"reduced_options": [1]}, {"enforce": "yes"}):
            with self.subTest(values=values):
                e._logged.clear()
                rules = lambda v=values: e.enforcement(values=lambda: v)  # noqa: E731
                self.put(state("quiet"))
                with self.assertLogs(e.logger, "ERROR") as logs:
                    for _ in range(2):
                        self.assertIsNone(e.decide("profile_task", START, now=NOW, data=self.tmp,
                                                   read_enforcement=rules))
                self.assertEqual(len(logs.output), 1)


class MessageTest(Case):
    """The return time is the tick's until_local; the hook computes none."""

    def message(self, st, args=START, tool="profile_task", now=NOW):
        return self.decide(tool, args, st=st, now=now)["message"]

    def test_until_local_is_quoted(self):
        msg = self.message(state("quiet", until=at("2026-11-25T11:15"), until_local="11:15"))
        self.assertIn("profile_task start is not allowed in quiet mode", msg)
        self.assertIn("Expected back at 11:15 (local).", msg)
        msg = self.message(state("reduced", until=at("2026-11-29T05:00"), until_local="Sun 05:00"),
                           tasks("Deep"), "delegate_task")
        self.assertIn("option Deep is not allowed in reduced mode; allowed: Fast, Balance", msg)
        self.assertIn("Expected back at Sun 05:00 (local).", msg)

    def test_older_tick_state_has_no_return_time(self):
        msg = self.message(state("essential", five_hour=80, seven_day=90))
        self.assertTrue(msg.endswith("profile_task start is not allowed in essential mode."), msg)
        self.assertNotIn("Expected back", msg)

    def test_past_until_is_not_quoted(self):
        msg = self.message(state("quiet", until=NOW - 60, until_local="09:59"))
        self.assertNotIn("Expected back", msg)

    def test_message_from_a_real_tick_two_windows_latest(self):
        def reading(now):
            return (80.0, 90.0, {"five_hour": at("2026-11-25T12:30"), "seven_day": at("2026-11-29T05:00")}), ""
        g.tick(now=NOW, data=self.tmp, read_settings_fn=lambda: S, reading=reading, out=io.StringIO())
        msg = e.decide("profile_task", START, now=NOW, data=self.tmp, read_enforcement=lambda: RULES)["message"]
        self.assertIn("in essential mode", msg)
        self.assertIn("Expected back at Sun 05:00 (local).", msg)

    def test_missing_option_says_why(self):
        msg = self.message(state("reduced"), {"goal": "g"}, "delegate_task")
        self.assertIn("without an option is not allowed in reduced mode", msg)
        self.assertIn("the default option is not visible to the governor", msg)


class OverrideExpiryTest(Case):
    def test_tick_after_until_allows_again(self):
        (self.tmp / "override.json").write_text(json.dumps({"mode": "quiet", "until": NOW + 60}))

        def reading(now):
            return (10.0, 10.0, {"five_hour": now + 3600, "seven_day": now + 86400}), ""
        g.tick(now=NOW, data=self.tmp, read_settings_fn=lambda: S, reading=reading, out=io.StringIO())
        self.assertTrue(self.blocked("profile_task", START, now=NOW))
        g.tick(now=NOW + 60, data=self.tmp, read_settings_fn=lambda: S, reading=reading, out=io.StringIO())
        self.assertFalse(self.blocked("profile_task", START, now=NOW + 60))


class FakeCtx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


class RegisterHookTest(unittest.TestCase):
    def default_profile(self, hook_missing):
        install = mock.Mock(return_value="created")
        patches = [mock.patch.object(plugin, "_is_default_profile", lambda: True),
                   mock.patch.object(plugin, "check_internals", lambda ctx=None: []),
                   mock.patch.object(plugin, "check_hook_internals", lambda ctx=None: hook_missing),
                   mock.patch.object(plugin, "install", install),
                   mock.patch.object(plugin, "_hermes_home", lambda: Path("/nonexistent")),
                   mock.patch.object(g, "schedule_enabled", lambda: True),
                   mock.patch.object(g, "data_dir", lambda: Path("/nonexistent"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return install

    def test_hook_internals_missing_never_stop_the_tick_install(self):
        install = self.default_profile(["hermes_cli.plugins.VALID_HOOKS[pre_tool_call]"])
        ctx = FakeCtx()
        with self.assertLogs(plugin.logger, "ERROR") as logs:
            plugin.register(ctx)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("VALID_HOOKS[pre_tool_call]", logs.output[0])
        install.assert_called_once()
        self.assertEqual(ctx.hooks, [("pre_tool_call", e.check)])

    def test_hook_not_registered_is_logged(self):
        install = self.default_profile([])
        with mock.patch.object(plugin, "register_hook", lambda ctx: False):
            with self.assertLogs(plugin.logger, "ERROR") as logs:
                plugin.register(FakeCtx())
        self.assertEqual(len(logs.output), 1)
        self.assertIn("hook is not registered", logs.output[0])
        install.assert_called_once()

    def test_non_default_profile_registers_hook_and_no_job(self):
        cron = mock.Mock()
        ctx = FakeCtx()
        with mock.patch.object(plugin, "_is_default_profile", lambda: False), \
                mock.patch.object(plugin, "check_hook_internals", lambda ctx=None: []), \
                mock.patch.object(plugin, "cron_api", cron), \
                mock.patch.object(plugin, "install", cron):
            plugin.register(ctx)
        self.assertEqual(ctx.hooks, [("pre_tool_call", e.check)])
        cron.assert_not_called()

    def test_manifest_declares_hook_and_settings(self):
        import yaml
        manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
        self.assertEqual(manifest["provides_hooks"], ["pre_tool_call"])
        self.assertEqual((DEFAULTS["enforce"], DEFAULTS["reduced_options"]), (True, ["Fast", "Balance"]))

    def test_hook_internals_name_a_ctx_without_hooks(self):
        self.assertIn("PluginContext.register_hook", plugin.check_hook_internals(object()))
        self.assertNotIn("PluginContext.register_hook", plugin.check_internals(object()))


@unittest.skipUnless(HAS_HERMES, "needs Hermes' Python (hermes_cli), see README Tests")
class RealTreeTest(unittest.TestCase):
    """Real Hermes path, config and dispatch code on a fake HOME + HERMES_HOME under /tmp."""

    TOOLSET = "mcp-probe-usage-governor"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="usage-governor-test-")
        self.addCleanup(tmp.cleanup)
        self.fake = Path(tmp.name)
        self.root = self.fake / ".hermes"
        self.profile = self.root / "profiles" / "coder"
        self.profile.mkdir(parents=True)
        self.data = self.root / "plugin-data" / "usage-governor"
        self.data.mkdir(parents=True)
        env = {"HOME": str(self.fake), "HERMES_HOME": str(self.profile), "HERMES_DATA_DIR_SUFFIX": ""}
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        e._logged.clear()

    def put(self, mode):
        (self.data / "state.json").write_text(json.dumps(state(mode)))

    def test_hook_reads_default_profile_state_from_another_profile(self):
        self.assertEqual((plugin.check_internals(FakeCtx()), plugin.check_hook_internals(FakeCtx())), ([], []))
        self.assertEqual(g.data_dir(), self.data)
        self.put("quiet")
        self.assertEqual(e.check("profile_task", START)["action"], "block")
        self.put("full")
        self.assertIsNone(e.check("profile_task", START))

    def test_profile_config_settings_apply(self):
        self.put("quiet")
        # Control: no profile setting, the hook blocks.
        self.assertEqual(e.check("profile_task", START)["action"], "block")
        (self.profile / "config.yaml").write_text(ENFORCE_FALSE)
        self.assertIsNone(e.check("profile_task", START))

    def test_default_profile_enforce_false_does_not_reach_another_profile(self):
        (self.root / "config.yaml").write_text(ENFORCE_FALSE)
        self.put("quiet")
        self.assertEqual(e.check("profile_task", START)["action"], "block")

    def _deferred_profile_task(self):
        from tools.registry import invalidate_check_fn_cache, registry
        ran = []

        def handler(args, task_id=None, **kw):
            ran.append(args)
            return json.dumps({"ok": True})
        registry.register(name="profile_task", handler=handler, toolset=self.TOOLSET,
                          schema={"name": "profile_task", "description": "probe",
                                  "parameters": {"type": "object", "properties": {}}})
        invalidate_check_fn_cache()
        self.addCleanup(invalidate_check_fn_cache)
        self.addCleanup(registry.deregister, "profile_task")
        return ran

    def test_wrapped_tool_call_reaches_hook_unwrapped(self):
        """Rule 7: handle_function_call unwraps tool_call before pre_tool_call."""
        import model_tools
        from hermes_cli import lifecycle
        ran = self._deferred_profile_task()
        seen = []

        def invoke_hook(name, **kw):
            if name != "pre_tool_call":
                return []
            seen.append(kw["tool_name"])
            got = e.check(**kw)
            return [got] if got else []
        self.put("quiet")
        with mock.patch.object(lifecycle, "invoke_hook", invoke_hook):
            result = model_tools.handle_function_call(
                "tool_call", {"name": "profile_task", "arguments": START}, enabled_toolsets=[self.TOOLSET],
                session_id="usage-governor-probe")
        self.assertEqual(seen, ["profile_task"])
        self.assertIn("BLOCKED by usage-governor", result)
        self.assertEqual(ran, [])

    def test_agent_loop_unwrap_gives_profile_task(self):
        """Rule 7: the agent loop peels tool_call before its pre_tool_call pass."""
        from agent import tool_executor
        self._deferred_profile_task()
        with mock.patch.object(tool_executor, "_tool_search_scoped_names", lambda agent: frozenset({"profile_task"})):
            name, args, block = tool_executor._unwrap_tool_search_call(
                None, "tool_call", {"name": "profile_task", "arguments": START})
        self.assertEqual((name, args, block), ("profile_task", START, None))

    def test_wrapper_reaching_hook_is_opened(self):
        self.put("quiet")
        got = e.check("tool_call", {"calls": [{"name": "profile_task", "arguments": START}]})
        self.assertEqual(got["action"], "block")


if __name__ == "__main__":
    unittest.main()
