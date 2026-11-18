"""Hook (b) of the MCP re-authentication notices: a target ``tool.complete`` frame whose result carries
``needs_reauth: true`` (``tools/mcp_tool_handlers.py`` ``_handle_auth_error_and_retry``) queues one notice per
server and task in the manager conversation; other results queue none, and approvals still end as before.

The e2e run cannot produce that result: the installed Hermes folds a tools/call 401 into a generic MCPError
(upstream #121285). This feeds the frame the gateway emits (``tui_gateway/tool_progress.py``: ``result`` parsed
from the tool's JSON) to the plugin's frame handler.

Run: ``PT_SCRIPT=tests/test_mcp_reauth.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes interpreter).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "profile-tasks" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("profile_tasks_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    profile_name = "default"
    _manager = None

    def __init__(self, accept: bool = True):
        from hermes_cli.plugins_state import PluginState
        self.state = PluginState("profile-tasks")
        self.accept = accept
        self.injected: list[tuple[str, str | None]] = []

    def get_config(self, key, default=None):
        return default

    def inject_message(self, text, session_key=None):
        if self.accept:
            self.injected.append((text, session_key))
        return self.accept


def frame(session_id: str, result, name: str = "mcp__linear__ro_probe") -> dict:
    return {"type": "tool.complete", "session_id": session_id,
            "payload": {"tool_id": "c1", "name": name, "is_error": True, "result": result}}


def reauth(server: str) -> dict:
    # tools.registry.tool_error(message, needs_reauth=True, server=...), parsed by the gateway.
    return {"error": f"MCP server '{server}' requires re-authentication. Run `hermes mcp login {server}`",
            "needs_reauth": True, "server": server}


def main() -> int:
    mod = load()
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    ctx = FakeCtx()
    plugin = mod.ProfileTasks(ctx, [])
    key = "agent:main:local:dm:manager"
    plugin.store.update("t-b", task_id="t-b", profile="implementer", session_id="s-impl", caller_session_key=key,
                        status="running", turns=["a"], created_at=time.time())

    plugin._on_target_frame("t-b", frame("s-impl", reauth("linear")))
    texts = [t for t, _ in ctx.injected]
    check(len(texts) == 1 and "hermes -p implementer mcp login linear" in texts[0] and "Task t-b (implementer)" in
          texts[0] and "'linear'" in texts[0] and ctx.injected[0][1] == key,
          "a needs_reauth tool result injects one notice naming the task, profile, server and login command into "
          "the manager conversation", injected=ctx.injected)

    plugin._on_target_frame("t-b", frame("s-impl", json.dumps(reauth("linear"))))
    plugin._on_target_frame("t-b", frame("s-impl", reauth("linear")))
    check(len(ctx.injected) == 1, "the same server again in the same task (dict or JSON string result) adds no notice",
          injected=len(ctx.injected))

    plugin._on_target_frame("t-b", frame("s-impl", reauth("notion"), name="mcp__notion__search"))
    check(len(ctx.injected) == 2 and "mcp login notion" in ctx.injected[1][0],
          "another server of the same task gets its own notice", injected=[t for t, _ in ctx.injected])

    for result in ({"error": "MCP call failed: MCPError: Server returned an error response"},
                   {"needs_reauth": False, "server": "jira"}, {"needs_reauth": True}, "plain text", None):
        plugin._on_target_frame("t-b", frame("s-impl", result))
    plugin._on_target_frame("t-b", frame("s-other", reauth("jira")))
    check(len(ctx.injected) == 2, "results without needs_reauth and a server, and frames of another session, add "
          "no notice", injected=len(ctx.injected))

    task = plugin.store.get("t-b")
    check(task.get("mcp_auth_noticed") == ["linear", "notion"] and not task.get("outbox")
          and [n["kind"] for n in task.get("notices") or []] == ["mcp_auth", "mcp_auth"],
          "the notices are recorded as delivered and the noticed servers kept in the task",
          noticed=task.get("mcp_auth_noticed"), notices=task.get("notices"))

    # A refused delivery keeps the notice for the next try, still only once per server.
    closed = FakeCtx(accept=False)
    other = mod.ProfileTasks(closed, [])
    other.store.update("t-c", task_id="t-c", profile="implementer", session_id="s-c", caller_session_key=key,
                       status="running", turns=["a"], created_at=time.time())
    other._on_target_frame("t-c", frame("s-c", reauth("linear")))
    other._on_target_frame("t-c", frame("s-c", reauth("linear")))
    kept = other.store.get("t-c").get("outbox") or []
    check(len(kept) == 1 and kept[0]["kind"] == "mcp_auth" and [a["ok"] for a in kept[0]["attempts"]] == [False],
          "refused by a closed manager conversation, the notice is kept as one entry for the next try; the repeated "
          "server queues nothing", kept=kept)

    # A gated tool's result still ends its approval; a needs_reauth result there does both.
    plugin.store.update("t-b", pending_approval={"prompt_id": "p1", "command": "rm -rf x", "raised_at": time.time(),
                                                 "timeout_s": 10.0, "expires_at": time.time() + 10})
    plugin._on_target_frame("t-b", frame("s-impl", reauth("atlassian"), name="mcp__atlassian__x"))
    task = plugin.store.get("t-b")
    check(not task.get("pending_approval") and (task.get("last_approval") or {}).get("outcome") == "ran"
          and "atlassian" in (task.get("mcp_auth_noticed") or []),
          "with an approval pending, the tool result still ends the approval and also notices the server",
          last_approval=task.get("last_approval"), noticed=task.get("mcp_auth_noticed"))

    # A 401 Hermes folds into a generic MCPError: mapped through the target profile's own connections.
    from tools import mcp_tool as core

    class Conn:
        def __init__(self, http: bool, status: int | None = None):
            self.http, self._http_rejection = http, ({"status": status} if status else {})
            self.session, self._park_reason = object(), None

        def _is_http(self):
            return self.http

    home = str(plugin._home("implementer"))
    conns = {"linear": Conn(True, 401), "jira": Conn(True), "gh": Conn(False), "slack": Conn(True, 500)}
    with core._lock:
        for name, conn in conns.items():
            core._servers[(home, name)] = conn
    generic = {"error": "MCP call failed: MCPError: Server returned an error response"}
    plugin.store.update("t-d", task_id="t-d", profile="implementer", session_id="s-d", caller_session_key=key,
                        status="running", turns=["a"], created_at=time.time())
    before = len(ctx.injected)
    plugin._on_target_frame("t-d", frame("s-d", json.dumps(generic)))
    plugin._on_target_frame("t-d", frame("s-d", generic))
    check(len(ctx.injected) == before + 1 and "a tool call failed with HTTP 401" in ctx.injected[-1][0]
          and "hermes -p implementer mcp login linear" in ctx.injected[-1][0],
          "a generic MCPError from mcp__linear__* whose connection last got a 401 gives one notice",
          injected=ctx.injected[before:])
    plugin._on_target_frame("t-d", frame("s-d", generic, name="mcp__slack__post"))
    plugin._on_target_frame("t-d", frame("s-d", {"error": "GitHub API 401 Unauthorized"}, name="mcp__gh__list"))
    plugin._on_target_frame("t-d", frame("s-d", {"error": "boom"}, name="mcp__jira__search"))
    plugin._on_target_frame("t-d", frame("s-d", {"ok": True}, name="mcp__jira__search"))
    plugin._on_target_frame("t-d", frame("s-d", {"error": "HTTP 401"}, name="terminal"))
    check(len(ctx.injected) == before + 1, "no notice for a 500, a stdio server relaying its API's 401, an error "
          "without a 401, a success, or a non-MCP tool", injected=ctx.injected[before + 1:])
    plugin._on_target_frame("t-d", frame("s-d", {"error": "HTTP 401 Unauthorized: invalid_token"},
                                         name="mcp__jira__search"))
    check(len(ctx.injected) == before + 2 and "mcp login jira" in ctx.injected[-1][0],
          "a remote server whose error text says 401/unauthorized gives a notice",
          injected=ctx.injected[before + 1:])

    # Set aside: an open breaker after 401s, or a park; a breaker without a 401 is not an auth problem.
    key_l, key_s = (home, "linear"), (home, "slack")
    core._server_error_counts[key_l] = core._server_error_counts[key_s] = 3
    core._server_breaker_opened_at[key_l] = core._server_breaker_opened_at[key_s] = time.monotonic()
    parked = Conn(True, 401)
    parked.session, parked._park_reason = None, "after reconnect retries exhausted"
    check(bool(mod._mcp_set_aside(key_l, conns["linear"])) and "paused it after 3" in
          mod._mcp_set_aside(key_l, conns["linear"]) and mod._mcp_set_aside(key_s, conns["slack"]) is None
          and "set it aside (after reconnect retries exhausted)" in (mod._mcp_set_aside((home, "p"), parked) or ""),
          "an open breaker or a park after a 401 is reported; a breaker opened by 500s is not",
          linear=mod._mcp_set_aside(key_l, conns["linear"]), parked=mod._mcp_set_aside((home, "p"), parked))
    core._server_breaker_opened_at[key_l] = time.monotonic() - core._CIRCUIT_BREAKER_COOLDOWN_SEC - 1
    check(mod._mcp_set_aside(key_l, conns["linear"]) is None, "a breaker past its cooldown (half-open) is not "
          "reported")

    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
