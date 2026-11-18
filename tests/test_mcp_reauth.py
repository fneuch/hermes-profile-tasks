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

    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
