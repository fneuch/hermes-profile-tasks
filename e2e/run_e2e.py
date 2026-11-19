"""End-to-end trials of the profile-tasks plugin against an isolated Hermes gateway.

Run through ``e2e/run.sh``: it sets a fake HOME, an isolated HERMES_HOME and lock dirs, so the
gateway started here never sees the real install. Two fake OpenAI-compatible providers stand in
for the models: one answers the manager (default profile), one answers the analyst profile.
Every check prints PASS/FAIL with its evidence; the full evidence is written as JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pwd
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from hermes_cli.observability.shared_metrics_consent import OFFER_VERSION
from tests.fakes.fake_llm_provider import MODEL_ID, FakeLLMServer, Text, ToolCall

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stop_gateway import alive, stop as stop_by_socket  # noqa: E402

HOME = Path(os.environ["HOME"])
HH = Path(os.environ["HERMES_HOME"])
REPO = Path(os.environ["PT_REPO"])
AGENT = Path(os.environ["PT_AGENT"])
REAL_ROOT = Path(pwd.getpwuid(os.getuid()).pw_dir, ".hermes").resolve()
assert HH.resolve() != REAL_ROOT and REAL_ROOT not in HH.resolve().parents, "refusing the real Hermes home"
assert HH == HOME / ".hermes"

FIXTURE_MCP = AGENT / "tests/e2e/core/parity/fixture_mcp_server.py"
MCP_TOOL = "mcp__atlassian__parity_canary"
CANARY = "ANALYST-ONLY-" + uuid.uuid4().hex[:10]
MANAGER_CANARY = "MANAGER-OWN-" + uuid.uuid4().hex[:10]
MANAGER_MCP_TOOL = "mcp__notion__parity_canary"
EVIDENCE: dict = {"base": str(HOME.parent), "canary": CANARY, "checks": []}
HTTP_FIXTURE_MCP = AGENT / "tests/e2e/core/mcp_plugins/mcp_fixture_server.py"
# Implementer MCP servers that need a new sign-in: one refuses every request, one refuses tool calls only.
LOGIN_SERVER, CALL_SERVER = "jira", "linear"
REAUTH_TOOL = f"mcp__{CALL_SERVER}__ro_probe"
WAIT_LIMIT = 25
HANG_SECONDS = 45


def log(*parts: object) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def check(test: str, ok: bool, what: str, **evidence: object) -> bool:
    EVIDENCE["checks"].append({"test": test, "ok": bool(ok), "what": what, **evidence})
    log("PASS" if ok else "FAIL", f"[{test}]", what, json.dumps(evidence, default=str)[:600])
    return ok


# ---- fake models ------------------------------------------------------------------------------


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def scenario_of(messages: list[dict]) -> tuple[str, int]:
    """(scenario tag of the current task turn, index of that user message)."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and "SCENARIO:" in text_of(m):
            tag = text_of(m).split("SCENARIO:", 1)[1].split()[0]
            return tag, i
    return "", -1


RELAY = {"on": False}
APPROVAL_TIMEOUT = 10
# Lets the trial close the manager conversation before the target raises its approval.
LATE_APPROVAL_DELAY = 12


def manager_model(record: dict):
    messages = record["body"]["messages"]
    last = messages[-1]
    if last.get("role") == "tool":
        return Text("MANAGER-ACK")
    text = text_of(last)
    if RELAY["on"] and "action=approval task_id=" in text:
        task_id = text.split("action=approval task_id=", 1)[1].split()[0].rstrip(":.,")
        return ToolCall("profile_task", {"action": "approval", "task_id": task_id})
    if "CALL " in text:
        return ToolCall("profile_task", json.loads(text.split("CALL ", 1)[1].strip()))
    return Text("MANAGER-WOKE")


def analyst_model(record: dict):
    messages = record["body"]["messages"]
    tag, at = scenario_of(messages)
    since = messages[at + 1:]
    tool_results = [m for m in since if m.get("role") == "tool"]
    after_user = [m for m in since if m.get("role") == "user"]
    reasoning = f"reasoning for {tag} step {len(tool_results)}"
    if tag == "MCP":
        if not tool_results:
            return ToolCall(MCP_TOOL, {"nonce": "jira-read"})
        return Text(f"MCP-RESULT {text_of(tool_results[-1])}", reasoning=reasoning)
    if tag.startswith("KEYED"):
        turns = sum(1 for m in messages if m.get("role") == "user" and "SCENARIO:KEYED" in text_of(m))
        return Text(f"KEYED-ANSWER {tag} user_turns={turns}", reasoning=reasoning)
    if tag.startswith("REMEMBER"):
        return Text(f"REMEMBERED {tag}", reasoning=reasoning)
    if tag == "RECALL":
        earlier = [text_of(m) for m in messages[:at] if m.get("role") == "user" and "SCENARIO:REMEMBER" in text_of(m)]
        token = earlier[-1].split("SCENARIO:", 1)[1].split()[0] if earlier else "NOTHING"
        return Text(f"RECALLED {token}", reasoning=reasoning)
    if tag == "LIVE":
        if not tool_results:
            time.sleep(4)
            return ToolCall(MCP_TOOL, {"nonce": "live"})
        return Text("LIVE-DONE", reasoning="live reasoning visible to a viewer", chunk_chars=4, delay_per_chunk=0.2)
    if tag == "LONG":
        if len(tool_results) < 4:
            time.sleep(2.5)
            return ToolCall(MCP_TOOL, {"nonce": f"long-{len(tool_results)}"})
        seen = any("CORRECTION-" in text_of(m) for m in since)
        return Text(f"LONG-DONE correction_seen={seen} user_rows_after_task={len(after_user)}", reasoning=reasoning)
    if tag == "LATE":
        if not tool_results and not after_user:
            return ToolCall(MCP_TOOL, {"nonce": "late"})
        if after_user and "CORRECTION-" in text_of(messages[-1]):
            return Text("LATE-SECOND-TURN handled the correction", reasoning=reasoning)
        return Text("LATE-FINAL " + "x" * 120, reasoning=reasoning, chunk_chars=4, delay_per_chunk=0.25)
    if tag.startswith("RELAY") or tag.startswith("APPROVAL"):
        if not tool_results and tag == "APPROVALLATE":
            time.sleep(LATE_APPROVAL_DELAY)
        if not tool_results:
            return ToolCall("terminal", {"command": f"rm -rf {HOME}/{tag.lower()}-probe-dir"})
        return Text(f"{tag}-RESULT {text_of(tool_results[-1])[:400]}", reasoning=reasoning)
    if tag.startswith("REAUTH"):
        # REAUTH: one call; REAUTHAGAIN: two more, which opens Hermes' circuit breaker (3 failures).
        if len(tool_results) < (2 if tag == "REAUTHAGAIN" else 1):
            return ToolCall(REAUTH_TOOL, {"nonce": f"{tag.lower()}-{len(tool_results)}"})
        return Text(f"{tag}-RESULT {text_of(tool_results[-1])[:300]}", reasoning=reasoning)
    if tag == "SLOW":
        time.sleep(WAIT_LIMIT + 15)
        return Text("SLOW-DONE", reasoning=reasoning)
    if tag.startswith("HANG"):
        time.sleep(HANG_SECONDS)
        return Text(f"{tag}-DONE", reasoning=reasoning)
    last = messages[-1]
    if last.get("role") == "user" and "CORRECTION-" in text_of(last):
        return Text("SECOND-TURN for correction", reasoning=reasoning)
    return Text("ANALYST-DEFAULT", reasoning=reasoning)


# ---- isolated home ------------------------------------------------------------------------------


def write_home(manager_url: str, analyst_url: str, remote: dict[str, str]) -> None:
    common = {
        "agent": {"api_max_retries": 1},
        "telemetry": {"shared_metrics": {"enabled": False, "send": False, "offer_version": OFFER_VERSION}},
        "updates": {"check": False},
        "logging": {"level": os.environ.get("PT_LOG_LEVEL", "INFO")},
        "mcp_discovery_timeout": 120,
        "mcp_single_query_discovery_timeout": 120,
    }

    def model(url: str) -> dict:
        return {"provider": "custom", "base_url": url, "default": MODEL_ID, "context_length": 128000}

    def fixture_mcp(canary: str, enabled: bool = True) -> dict:
        return {"command": sys.executable, "args": [str(FIXTURE_MCP)], "enabled": enabled,
                "env": {"PARITY_MCP_CANARY": canary, "PYTHONPATH": str(AGENT)},
                "connect_timeout": 60, "timeout": 60}

    # Like the live default profile: enabled servers of its own, and a disabled `atlassian` entry.
    default = dict(common, model=model(manager_url), gateway={"multiplex_profiles": True}, plugins={
        "enabled": ["profile-tasks", "pt-probe"],
        "entries": {"profile-tasks": {"allow_gateway_injection": True,
                                      "settings": {"wait_timeout_seconds": WAIT_LIMIT}}},
    }, mcp_servers={"notion": fixture_mcp(MANAGER_CANARY), "atlassian": fixture_mcp(MANAGER_CANARY, enabled=False)})
    analyst = dict(common, model=model(analyst_url), display={"show_reasoning": True},
                   approvals={"mode": "smart", "timeout": APPROVAL_TIMEOUT},
                   mcp_servers={"atlassian": {"command": sys.executable, "args": [str(FIXTURE_MCP)],
                                              "env": {"PARITY_MCP_CANARY": CANARY, "PYTHONPATH": str(AGENT)},
                                              "connect_timeout": 60, "timeout": 60}})
    implementer = dict(common, model=model(analyst_url), mcp_servers={
        name: {"url": url, "connect_timeout": 15, "timeout": 30} for name, url in remote.items()})
    homes = {"default": HH, "analyst": HH / "profiles/analyst", "implementer": HH / "profiles/implementer"}
    for name, cfg in (("default", default), ("analyst", analyst), ("implementer", implementer)):
        homes[name].mkdir(parents=True, exist_ok=True)
        (homes[name] / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        (homes[name] / ".env").write_text("OPENAI_API_KEY=sk-fake-e2e\n", encoding="utf-8")
    (HH / "plugins").mkdir(exist_ok=True)
    (HH / "plugins/profile-tasks").symlink_to(REPO / "profile-tasks")
    (HH / "plugins/pt-probe").symlink_to(REPO / "e2e/probe-plugin")
    EVIDENCE["configs"] = {"default": default, "analyst": analyst, "implementer": implementer}


class _Unauthorized(BaseHTTPRequestHandler):
    """A remote MCP endpoint whose sign-in is rejected: every request gets 401."""

    def _refuse(self) -> None:
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = _refuse

    def log_message(self, *args) -> None:
        EVIDENCE.setdefault("unauthorized_requests", []).append(args[0] % args[1:])


def _die_with_parent() -> None:
    import ctypes
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def start_remote_mcp() -> tuple[dict[str, str], list]:
    """The implementer's two remote MCP servers; returns their URLs and what to stop."""
    refuser = ThreadingHTTPServer(("127.0.0.1", 0), _Unauthorized)
    threading.Thread(target=refuser.serve_forever, name="e2e-401", daemon=True).start()
    root = HOME.parent / "mcp-linear"
    root.mkdir(exist_ok=True)
    (root / "401_budget").write_text("1000", encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(HOME), "PYTHONPATH": str(AGENT), "MCPE2E_TRANSPORT": "http",
           "MCPE2E_PORT_FILE": str(root / "port"), "MCPE2E_LOG": str(root / "inbound.jsonl"),
           "MCPE2E_401_CALLS": str(root / "401_budget"), "MCPE2E_NAME": CALL_SERVER}
    err = open(root / "stderr", "wb")
    proc = subprocess.Popen([sys.executable, str(HTTP_FIXTURE_MCP)], env=env, stdin=subprocess.DEVNULL,
                            stdout=err, stderr=err, preexec_fn=_die_with_parent)
    deadline = time.monotonic() + 60
    while not (root / "port").exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"HTTP MCP fixture did not start: {(root / 'stderr').read_text()[-500:]}")
        time.sleep(0.2)
    port = (root / "port").read_text().split()[0]
    EVIDENCE["remote_mcp"] = {"inbound_log": str(root / "inbound.jsonl")}
    return ({LOGIN_SERVER: f"http://127.0.0.1:{refuser.server_address[1]}/mcp",
             CALL_SERVER: f"http://127.0.0.1:{port}/mcp"}, [refuser, proc])


def stop_remote_mcp(handles: list) -> None:
    refuser, proc = handles
    refuser.shutdown()
    refuser.server_close()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(10)


# ---- gateway ------------------------------------------------------------------------------------


def start_gateway() -> subprocess.Popen:
    log_path = HOME.parent / "gateway.log"
    out = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, "-m", "gateway.run"],
                            cwd=AGENT, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
    EVIDENCE["gateway_log"] = str(log_path)
    return proc


def stop_gateway(proc: subprocess.Popen | None) -> None:
    """Stop whatever gateway owns the isolated home (its control socket), then the child we started."""
    with contextlib.suppress(Exception):
        stop_by_socket(HH)
    if proc is not None and proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGTERM)
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(10)


class Client:
    """A desktop-like websocket client of one profile's gateway that records every frame."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.home = HH if profile == "default" else HH / "profiles" / profile
        self.frames: list[dict] = []

    async def __aenter__(self):
        from hermes_cli.gateway_client import GatewayClient, _session_ticket, gateway_ws_target
        from hermes_cli.gateway_runtime import ensure_gateway_runtime
        from websockets.asyncio.client import connect
        deadline = time.monotonic() + 300
        while True:
            ready = await asyncio.to_thread(ensure_gateway_runtime, self.home)
            if ready.state == "ready" or time.monotonic() > deadline:
                break
            await asyncio.sleep(2)
        assert ready.state == "ready", (ready.state, ready.reason_code)
        ticket = await asyncio.to_thread(_session_ticket, self.home, ready.endpoint)
        url, protocols = gateway_ws_target(ready.endpoint, ticket)
        self._stack = contextlib.AsyncExitStack()
        ws = await self._stack.enter_async_context(connect(url, subprotocols=protocols, max_size=8 << 20, proxy=None))
        self.client = await self._stack.enter_async_context(GatewayClient(ws))
        self._pump = asyncio.create_task(self._drain())
        return self

    async def _drain(self):
        while True:
            event = await self.client.events.get()
            if isinstance(event, Exception):
                return
            self.frames.append(dict(event.get("params") or {}, _t=time.time()))

    async def __aexit__(self, *exc):
        self._pump.cancel()
        await self._stack.aclose()

    async def rpc(self, method: str, **params):
        return await self.client.rpc(method, **params)

    def types(self, session_id: str | None = None) -> list[str]:
        return [f.get("type") for f in self.frames if session_id is None or f.get("session_id") == session_id]


async def until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise TimeoutError(what)


def tool_result_of(server: FakeLLMServer, action: str, after: int = 0) -> dict | None:
    """The profile_task result the manager model received for its last *action* call."""
    for body in reversed(server.main_requests()[after:]):
        msgs = body["messages"]
        if msgs[-1].get("role") == "tool":
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    call = m["tool_calls"][-1]["function"]
                    name, args = call["name"], json.loads(call["arguments"])
                    if name == "tool_call":
                        name, args = args["calls"][-1]["name"], args["calls"][-1]["arguments"]
                    if name == "profile_task" and args.get("action") == action:
                        try:
                            return json.loads(text_of(msgs[-1]))
                        except json.JSONDecodeError:
                            return {"error": text_of(msgs[-1])}
                    break
    return None


def manager_got(server: FakeLLMServer, needle: str) -> str | None:
    for body in server.main_requests():
        last = body["messages"][-1]
        if last.get("role") == "user" and needle in text_of(last):
            return text_of(last)
    return None


def manager_saw(server: FakeLLMServer, needle: str) -> str | None:
    """The first non-assistant message, at any position of any manager model request, that contains *needle*.

    A message injected while a manager turn runs can reach the model inside that turn, not as a user message.
    """
    return next((text_of(m) for body in server.main_requests() for m in body["messages"]
                 if m.get("role") != "assistant" and needle in text_of(m)), None)


def analyst_db(sql: str, args: tuple = ()) -> list[tuple]:
    with sqlite3.connect(f"file:{HH / 'profiles/analyst/state.db'}?mode=ro", uri=True) as db:
        return db.execute(sql, args).fetchall()


def tool_names(body: dict) -> list[str]:
    return sorted(t["function"]["name"] for t in body.get("tools") or [])


def offered_mcp(body: dict) -> list[str]:
    """MCP tool names offered in a request, directly or in the tool-search bridge catalog."""
    import re
    return sorted(set(re.findall(r"mcp__[a-z0-9_]+", json.dumps(body.get("tools") or []))))


# ---- scenarios ----------------------------------------------------------------------------------


async def manager_session(mgr: Client) -> str:
    snap = await mgr.rpc("session.create", source="gui", request_id=uuid.uuid4().hex)
    return snap["session_id"]


async def say(mgr: Client, sid: str, text: str) -> dict:
    return await mgr.rpc("prompt.submit", session_id=sid, text=text, submission_id=uuid.uuid4().hex)


async def start_task(ctx, mgr: Client, sid: str, scenario: str, title: str, profile: str = "analyst") -> dict:
    before = len(ctx["m"].main_requests())
    args = {"action": "start", "profile": profile, "title": title, "text": f"SCENARIO:{scenario} please do it"}
    await say(mgr, sid, "CALL " + json.dumps(args))
    return await until(lambda: tool_result_of(ctx["m"], "start", before), 90, f"start ack for {scenario}")


async def trial_1_2_3(ctx) -> None:
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        t0 = time.monotonic()
        ack = await start_task(ctx, mgr, sid, "MCP", "jira read")
        ack_seconds = round(time.monotonic() - t0, 2)
        task_id = ack.get("task_id")
        check("2", ack.get("accepted") is True and ack.get("session_id"), "tool returns at once with task id, "
              "profile, session id and title", ack=ack, ack_seconds=ack_seconds)
        wake = await until(lambda: manager_got(ctx["m"], f"for task {task_id}"), 90, "wake message")
        check("2", CANARY in wake, "the forwarded task came back from a plugin thread inside the gateway",
              wake_message=wake[:400])
        check("1", CANARY in wake, "the answer carries the result of the analyst-only MCP tool (its own env token)",
              canary=CANARY)
        mgr_tools = sorted({n for b in ctx["m"].main_requests() for n in tool_names(b)})
        mgr_mcp = sorted({n for b in ctx["m"].main_requests() for n in offered_mcp(b)})
        ana_mcp = sorted({n for b in ctx["a"].main_requests() for n in offered_mcp(b)})
        mgr_catalog = json.dumps(ctx["m"].main_requests()[0].get("tools") or [])
        check("1", MCP_TOOL in ana_mcp and MANAGER_MCP_TOOL in mgr_mcp
              and not any(n.startswith("mcp__atlassian__") for n in mgr_mcp) and MANAGER_MCP_TOOL not in ana_mcp,
              "with MCP servers of its own (notion enabled, atlassian disabled), the manager is offered only its "
              "own enabled server's tools and none of the analyst's; the analyst is offered its atlassian tool",
              analyst_mcp_tools=ana_mcp, manager_mcp_tools=mgr_mcp, manager_direct_tools=mgr_tools,
              manager_catalog_has_profile_task="profile_task" in mgr_catalog,
              manager_mcp_servers_enabled={n: c["enabled"] for n, c in
                                           EVIDENCE["configs"]["default"]["mcp_servers"].items()})
        await until(lambda: any(f.get("type") == "message.complete" and "MANAGER-WOKE" in json.dumps(f)
                                for f in mgr.frames), 60, "manager woke")
        woke = [f for f in mgr.frames if f.get("type") == "message.complete"]
        check("3", any("MANAGER-WOKE" in json.dumps(f) for f in woke),
              "the injected answer started a manager turn on its own and the desktop client received it",
              manager_session=sid, completes=len(woke))
        snap = await mgr.rpc("session.resume", session_id=sid)
        rows = [(m.get("role"), str(m.get("content") or m.get("text") or "")[:120]) for m in snap["messages"]]
        check("3", any(f"for task {task_id}" in r[1] for r in rows), "the answer is a message of the manager "
              "transcript", transcript=rows)
        ctx["task1"] = ack


async def trial_4(ctx) -> None:
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        token = "REMEMBER-" + uuid.uuid4().hex[:6]
        ack = await start_task(ctx, mgr, sid, token, "context check")
        task_id = ack["task_id"]
        await until(lambda: manager_got(ctx["m"], f"for task {task_id}"), 90, "first answer")
        before = len(ctx["m"].main_requests())
        await say(mgr, sid, "CALL " + json.dumps({"action": "follow_up", "task_id": task_id,
                                                  "text": "SCENARIO:RECALL what did I ask you to remember?"}))
        follow = await until(lambda: tool_result_of(ctx["m"], "follow_up", before), 90, "follow-up ack")
        answer = await until(lambda: manager_got(ctx["m"], f"RECALLED {token}"), 90, "recall answer")
        sessions = analyst_db("SELECT id, title, source FROM sessions WHERE title LIKE ?", (f"%{task_id}%",))
        check("4", follow.get("session_id") == ack["session_id"] and len(sessions) == 1 and token in answer,
              "the follow-up ran in the same analyst session with its context; one session for the task",
              session_start=ack["session_id"], session_follow_up=follow.get("session_id"),
              analyst_sessions_for_task=sessions, answer=answer[:300])


async def trial_5(ctx) -> None:
    async with Client("default") as mgr, Client("analyst") as viewer:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "APPROVAL", "approval probe")
        task_id, asid = ack["task_id"], ack["session_id"]
        await viewer.rpc("session.resume", session_id=asid, omit_messages=True)
        t0 = time.monotonic()
        wake = await until(lambda: manager_got(ctx["m"], f"for task {task_id}"), 120, "approval task end")
        waited = round(time.monotonic() - t0, 1)
        approvals = [f for f in viewer.frames if str(f.get("type", "")).startswith("approval")]
        probe_exists = (HOME / "approval-probe-dir").exists()
        check("5", True, "observed: what a flagged command does in an unwatched task session",
              approval_frames=[{"type": f.get("type"), "payload": f.get("payload")} for f in approvals][:4],
              seconds_to_answer=waited, wake_message=wake[:600], probe_dir_exists_after=probe_exists,
              aux_requests_to_analyst_model=len(ctx["a"].aux_requests()))


async def answer_of(ctx, mgr: Client, sid: str, task_id: str) -> str:
    """The task's final answer: injected if it came within the wait limit, else read through status."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        injected = manager_got(ctx["m"], f"Answer from analyst for task {task_id}")
        if injected:
            return injected
        row = (await ask_status(ctx, mgr, sid, task_id))["tasks"][0]
        if row.get("answer"):
            return row["answer"]
        await asyncio.sleep(5)
    raise TimeoutError(f"answer of {task_id}")


def plugin_task(task_id: str) -> dict:
    """The task as the plugin stored it (isolated home's plugin state file)."""
    for path in (HH / "plugin-data").glob("*/state.json"):
        tasks = json.loads(path.read_text()).get("tasks") or {}
        if task_id in tasks:
            return tasks[task_id]
    return {}


# How each target-side ending is caused, the outcome the plugin must record, and the notice words.
APPROVAL_ENDINGS = {
    "APPROVAL": ("expire", ("timeout",), "expired unanswered", True),
    "APPROVALOK": ("approve", ("ran",), "was approved in the analyst session; the command ran", False),
    "APPROVALDENY": ("deny", ("denied",), "was denied in the analyst session; the command did not run", True),
    "APPROVALINT": ("interrupt", ("cancelled", "interrupted"), "did not run", True),
}


async def trial_approval(ctx) -> None:
    """A target approval is reported at once; its real ending is reported; it can be answered from the manager."""
    for tag, (how, outcomes, words, probe_stays) in APPROVAL_ENDINGS.items():
        probe = HOME / f"{tag.lower()}-probe-dir"
        async with Client("default") as mgr, Client("analyst") as viewer:
            sid = await manager_session(mgr)
            probe.mkdir(exist_ok=True)
            ack = await start_task(ctx, mgr, sid, tag, f"approval {how}")
            task_id, asid = ack["task_id"], ack["session_id"]
            await viewer.rpc("session.resume", session_id=asid, omit_messages=True)
            notice = await until(lambda: manager_got(ctx["m"], f"Task {task_id} (analyst) is waiting for an approval"),
                                 APPROVAL_TIMEOUT, "approval notice")
            notice_at = time.time()
            acted = None
            if how in ("approve", "deny"):
                request = await until(lambda: next((f for f in viewer.frames if f.get("type") == "approval.request"
                                                    and f.get("session_id") == asid), None), 10, "target prompt")
                acted = await viewer.rpc("approval.respond", session_id=asid,
                                         execution_generation=request["payload"]["execution_generation"],
                                         prompt_id=request["payload"]["prompt_id"],
                                         choice="once" if how == "approve" else "deny")
            elif how == "interrupt":
                request = await until(lambda: next((f for f in viewer.frames if f.get("type") == "approval.request"
                                                    and f.get("session_id") == asid), None), 10, "target prompt")
                acted = await viewer.rpc("session.interrupt", session_id=asid,
                                         execution_generation=request["payload"]["execution_generation"])
            ended = await until(lambda: manager_got(ctx["m"], f"Task {task_id} (analyst): the approval for"),
                                APPROVAL_TIMEOUT + 60, "approval ending notice")
            ended_at = time.time()
            stored = await until(lambda: plugin_task(task_id).get("last_approval"), 30, "stored approval outcome")
            check("approval", f"hermes://open/{asid}" in notice and probe.name in notice
                  and "action=approval" in notice and (how != "expire" or ended_at - notice_at > APPROVAL_TIMEOUT - 3),
                  f"approval then {how}: the manager got the command, task, session and link while it was answerable",
                  notice=notice, seconds_notice_before_ending=round(ended_at - notice_at, 1))
            check("approval", stored.get("outcome") in outcomes and words in ended
                  and probe.exists() == probe_stays,
                  f"approval then {how} in the analyst session: the manager is told the real ending "
                  f"({'/'.join(outcomes)}), read from the gated command's result",
                  target_frames=[(f.get("type"), (f.get("payload") or {}).get("result", {}).get("status")
                                  if isinstance((f.get("payload") or {}).get("result"), dict) else None)
                                 for f in viewer.frames if f.get("session_id") == asid
                                 and str(f.get("type")).split(".")[0] in ("approval", "tool", "message")
                                 and f.get("type") != "message.delta"][:30],
                  target_action=acted, ending_notice=ended, stored_outcome=stored.get("outcome"),
                  stored_user_summary=stored.get("user_summary"), probe_dir_exists=probe.exists())
    # Relayed: the prompt opens in the manager conversation, the answer there runs the command.
    RELAY["on"] = True
    try:
        # Relay card left open while the target request expires: the card is withdrawn, nothing is written back.
        probe = HOME / "relayexpire-probe-dir"
        async with Client("default") as mgr:
            sid = await manager_session(mgr)
            probe.mkdir(exist_ok=True)
            before = len(ctx["m"].main_requests())
            ack = await start_task(ctx, mgr, sid, "RELAYEXPIRE", "approval relay left open")
            task_id, asid = ack["task_id"], ack["session_id"]
            card = await until(lambda: next((f for f in mgr.frames if f.get("type") == "approval.request"
                                             and f.get("session_id") == sid), None), APPROVAL_TIMEOUT,
                               "manager card")
            card_at = time.time()
            payload = card["payload"]
            relayed = await until(lambda: tool_result_of(ctx["m"], "approval", before), APPROVAL_TIMEOUT + 60,
                                  "approval tool result")
            # A withdrawn card ends the manager-side wait with choice "cancelled"; its own expiry would be "timeout".
            frames_after = [f.get("type") for f in mgr.frames if f.get("session_id") == sid and f["_t"] >= card_at]
            replay = await mgr.rpc("session.events.since", session_id=sid, last_seen=0)
            still_open = [r for r in replay.get("open_requests") or [] if r.get("method") == "approval"]
            state_at_end = plugin_task(task_id)
            from hermes_cli.gateway_client import GatewayRPCError
            try:
                dead = await mgr.rpc("approval.respond", session_id=sid,
                                     execution_generation=payload["execution_generation"],
                                     prompt_id=payload["prompt_id"], choice="once")
            except GatewayRPCError as exc:
                dead = {"refused": f"{exc.__class__.__name__}: {exc}"}
            await asyncio.sleep(3)
            state_after = plugin_task(task_id)
            ended = await until(lambda: manager_saw(ctx["m"], f"Task {task_id} (analyst): the approval for"), 120,
                                "approval ending notice")
            last = state_after.get("last_approval") or {}
            check("approval", not still_open and relayed.get("forwarded") is False
                  and relayed.get("manager_choice") == "cancelled"
                  and state_after.get("pending_approval") is None and last.get("outcome") == "timeout"
                  and not last.get("relayed") and "expired unanswered" in ended and probe.exists(),
                  "relay card open while the target request expired: the card was withdrawn from the manager "
                  "conversation, the approval tool reported nothing forwarded, answering the dead card wrote no "
                  "pending approval back, and the manager got the real ending (timeout)",
                  seconds_card_open=round(time.time() - card_at, 1), manager_frames_after_card=frames_after,
                  manager_open_approval_requests=still_open,
                  relay_result=relayed, dead_card_respond=dead, pending_at_end=state_at_end.get("pending_approval"),
                  pending_after_dead_answer=state_after.get("pending_approval"), last_approval=last,
                  ending_notice=ended[:300], probe_dir_exists=probe.exists(), analyst_session=asid)
        for choice, tag in (("once", "RELAY"), ("always", "RELAYALWAYS")):
            probe = HOME / f"{tag.lower()}-probe-dir"
            async with Client("default") as mgr:
                sid = await manager_session(mgr)
                probe.mkdir(exist_ok=True)
                ack = await start_task(ctx, mgr, sid, tag, f"approval relay {choice}")
                task_id, asid = ack["task_id"], ack["session_id"]
                t0 = time.time()
                await until(lambda: manager_got(ctx["m"], f"Task {task_id} (analyst) is waiting for an approval"),
                            APPROVAL_TIMEOUT, "relay notice")
                notice_at = time.time()
                card = await until(lambda: next((f for f in mgr.frames if f.get("type") == "approval.request"
                                                 and f.get("session_id") == sid), None), APPROVAL_TIMEOUT,
                                   "manager card")
                card_at = time.time()
                payload = card["payload"]
                before = len(ctx["m"].main_requests())
                respond = await mgr.rpc("approval.respond", session_id=sid,
                                        execution_generation=payload["execution_generation"],
                                        prompt_id=payload["prompt_id"], choice=choice)
                relayed = await until(lambda: tool_result_of(ctx["m"], "approval", before), 120,
                                      "approval tool result")
                final = await answer_of(ctx, mgr, sid, task_id)
                text = str(payload.get("command")) + str(payload.get("description"))
                check("approval", card_at - t0 < APPROVAL_TIMEOUT and probe.name in text
                      and "'always' permanently allows that kind of command in the analyst profile" in text
                      and "one inert rule for this prompt in the manager profile's config" in text,
                      f"the approval prompt itself opened in the manager conversation before the target request "
                      f"expired, and its text states what 'always' does ({choice} run)",
                      manager_card=payload, seconds_notice=round(notice_at - t0, 1),
                      seconds_card=round(card_at - t0, 1), analyst_session=asid, manager_session=sid)
                check("approval", relayed.get("forwarded") is True and relayed.get("choice") == choice
                      and '"exit_code": 0' in final and not probe.exists(),
                      f"{choice} clicked in the manager conversation: the answer reached the analyst session and "
                      f"the command ran", respond=respond, relay_result=relayed, answer=final[:400],
                      probe_dir_exists=probe.exists())
                if choice == "always":
                    target_rules = (yaml.safe_load((HH / "profiles/analyst/config.yaml").read_text()) or {}).get(
                        "command_allowlist") or []
                    manager_rules = (yaml.safe_load((HH / "config.yaml").read_text()) or {}).get(
                        "command_allowlist") or []
                    relay_rules = [r for r in manager_rules if "profile_task_relay" in str(r)]
                    check("approval", len(target_rules) >= 1 and relay_rules == [
                          f"plugin_rule:profile_task_relay:{plugin_task(task_id)['last_approval']['prompt_id']}"]
                          and not any("profile_task_relay" in str(r) for r in target_rules),
                          "'always' relayed: the analyst profile's command_allowlist now allows that kind of command, "
                          "and the manager config holds exactly one inert rule keyed to that prompt",
                          analyst_command_allowlist=target_rules, manager_command_allowlist=manager_rules)
    finally:
        RELAY["on"] = False


async def trial_6(ctx) -> None:
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "LONG", "long task")
        task_id, asid = ack["task_id"], ack["session_id"]
        n0 = len(ctx["a"].main_requests())
        await until(lambda: len(ctx["a"].main_requests()) >= n0 + 1, 30, "long task first step")
        correction = "CORRECTION-" + uuid.uuid4().hex[:6] + " focus on the second ticket"
        before = len(ctx["m"].main_requests())
        await say(mgr, sid, "CALL " + json.dumps({"action": "correct", "task_id": task_id, "text": correction}))
        result = await until(lambda: tool_result_of(ctx["m"], "correct", before), 60, "correct ack")
        wake = await until(lambda: manager_got(ctx["m"], f"for task {task_id}"), 120, "long task answer")
        rows = analyst_db("SELECT role, substr(content, 1, 200) FROM messages WHERE session_id IN "
                          "(SELECT id FROM sessions WHERE title LIKE ?) ORDER BY id", (f"%{task_id}%",))
        in_session = [r for r in rows if r[1] and correction.split()[0] in r[1]]
        check("6", result.get("corrected") is True and "correction_seen=True" in wake and bool(in_session),
              "the correction reached the running turn, the final answer used it, and it is a message of "
              "the analyst session", correct_result=result, wake_message=wake[:300], correction_rows=in_session,
              analyst_session=asid)


async def trial_7(ctx) -> None:
    async with Client("default") as mgr, Client("analyst") as viewer:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "LATE", "late correction")
        task_id, asid = ack["task_id"], ack["session_id"]
        await viewer.rpc("session.resume", session_id=asid, omit_messages=True)
        await until(lambda: "LATE-FINAL" in "".join(str((f.get("payload") or {}).get("text") or "")
                                                    for f in viewer.frames if f.get("type") == "message.delta"),
                    60, "final answer streaming")
        correction = "CORRECTION-" + uuid.uuid4().hex[:6] + " add a closing line"
        before = len(ctx["m"].main_requests())
        await say(mgr, sid, "CALL " + json.dumps({"action": "correct", "task_id": task_id, "text": correction}))
        result = await until(lambda: tool_result_of(ctx["m"], "correct", before), 60, "late correct ack")
        wake = await until(lambda: manager_got(ctx["m"], f"for task {task_id}"), 120, "late task answer")
        await asyncio.sleep(15)
        second = [b for b in ctx["a"].main_requests()
                  if b["messages"][-1].get("role") == "user" and correction.split()[0] in text_of(b["messages"][-1])]
        rows = analyst_db("SELECT role, substr(content, 1, 120) FROM messages WHERE session_id IN "
                          "(SELECT id FROM sessions WHERE title LIKE ?) ORDER BY id", (f"%{task_id}%",))
        completes = [f.get("payload", {}).get("admission_id") for f in viewer.frames if f.get("type") == "message.complete"]
        check("7", True, "observed: a correction sent while the analyst writes its final answer",
              correct_result=result, wake_message=wake[:200], second_turn_requests=len(second),
              completes_in_session=completes, transcript=rows)


async def trial_8(ctx) -> None:
    async with Client("default") as mgr, Client("analyst") as viewer:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "LIVE", "live view")
        task_id, asid = ack["task_id"], ack["session_id"]
        listed = await viewer.rpc("session.list")
        rows = listed.get("sessions") if isinstance(listed, dict) else listed
        in_list = [r for r in rows or [] if r.get("id") == asid or r.get("session_id") == asid]
        await viewer.rpc("session.resume", session_id=asid, omit_messages=True)
        await until(lambda: manager_got(ctx["m"], f"for task {task_id}"), 90, "live task answer")
        kinds = [t for t in viewer.types(asid)]
        if not kinds:
            kinds = viewer.types()
        reasoning_frames = [f for f in viewer.frames if f.get("type") in ("reasoning.delta", "reasoning.available",
                                                                            "thinking.delta")]
        tool_frames = [f.get("type") for f in viewer.frames if str(f.get("type", "")).startswith("tool.")]
        stored = analyst_db("SELECT role, length(reasoning), substr(reasoning,1,60) FROM messages WHERE session_id IN "
                            "(SELECT id FROM sessions WHERE title LIKE ?) AND role='assistant'", (f"%{task_id}%",))
        src = analyst_db("SELECT source, title FROM sessions WHERE title LIKE ?", (f"%{task_id}%",))
        check("8", bool(in_list), "the task session is in the analyst profile's session list",
              listed=in_list[:1], source_and_title=src)
        check("8", bool(tool_frames) and bool(reasoning_frames),
              "a viewer that opened the session mid-task received tool calls and reasoning live",
              tool_frames=tool_frames, reasoning_frames=len(reasoning_frames),
              first_reasoning=reasoning_frames[0].get("payload") if reasoning_frames else None,
              frame_types=sorted(set(kinds)))
        check("8", any(r[1] for r in stored), "the reasoning is stored with the assistant messages",
              stored=stored)
        # A second task nobody opens: its reasoning is stored too, so opening is not what enables it.
        ack2 = await start_task(ctx, mgr, sid, "MCP", "unwatched")
        await until(lambda: manager_got(ctx["m"], f"for task {ack2['task_id']}"), 90, "unwatched answer")
        stored2 = analyst_db("SELECT role, length(reasoning) FROM messages WHERE session_id IN "
                             "(SELECT id FROM sessions WHERE title LIKE ?) AND role='assistant'",
                             (f"%{ack2['task_id']}%",))
        check("8", any(r[1] for r in stored2), "an unwatched task session stores its reasoning too", stored=stored2)


async def probe(op: str, **args) -> dict:
    """Run profile_tasks.<op>(**args) in a gateway thread that has no conversation (the pt-probe plugin)."""
    box = HH / "pt-probe"
    box.mkdir(exist_ok=True)
    name = uuid.uuid4().hex[:8]
    (box / f".{name}").write_text(json.dumps({"op": op, "args": args}))
    (box / f".{name}").rename(box / f"request-{name}.json")
    response = box / f"response-{name}.json"
    await until(response.exists, 180, f"probe {op}")
    out = json.loads(response.read_text())
    EVIDENCE.setdefault("probe_calls", []).append({"t": time.strftime("%H:%M:%S"), "op": op, "args": args,
                                                   **{k: v for k, v in out.items() if k != "trace"}})
    if not out["ok"]:
        raise RuntimeError(f"probe {op}: {out['error']}\n{out.get('trace', '')}")
    return out["value"]


async def trial_keyed(ctx) -> None:
    """send/result from a plugin backend: one session per key, follow-ups reuse it, the answer is returned."""
    a, b = "desk-pr-" + uuid.uuid4().hex[:6], "desk-pr-" + uuid.uuid4().hex[:6]
    before = len(ctx["m"].main_requests())
    first = await probe("send", key=a, profile="analyst", text="SCENARIO:KEYEDA1 first question", title="PR A")
    second = await probe("send", key=b, profile="analyst", text="SCENARIO:KEYEDB1 other question", title="PR B")
    check("keyed", first.get("accepted") is True and second.get("accepted") is True
          and first["session_id"] != second["session_id"] and "note" not in first,
          "two keys from a caller with no conversation start two tasks in two analyst sessions",
          first=first, second=second)
    answer_a = await probe("result", key=a, wait=90)
    answer_b = await probe("result", key=b, wait=90)
    check("keyed", answer_a.get("status") == "answered" and answer_a.get("answer", "").startswith(
          "KEYED-ANSWER KEYEDA1") and answer_b.get("answer", "").startswith("KEYED-ANSWER KEYEDB1"),
          "result returns each key's final answer and status", result_a=answer_a, result_b=answer_b)
    follow = await probe("send", key=a, profile="analyst", text="SCENARIO:KEYEDA2 follow-up")
    answer_a2 = await probe("result", key=a, wait=90)
    sessions = analyst_db("SELECT id, title, source FROM sessions WHERE id IN (?, ?)",
                          (first["session_id"], second["session_id"]))
    check("keyed", follow.get("task_id") == first["task_id"] and follow.get("session_id") == first["session_id"]
          and answer_a2.get("answer") == "KEYED-ANSWER KEYEDA2 user_turns=2" and len(sessions) == 2,
          "a second send on the first key is a follow-up in its session, with the first turn as context",
          follow=follow, result=answer_a2, analyst_sessions=sessions)
    async with Client("analyst") as viewer:
        listed = await viewer.rpc("session.list")
    rows = listed.get("sessions") if isinstance(listed, dict) else listed
    ids = {r.get("id") for r in rows or []}
    check("keyed", {first["session_id"], second["session_id"]} <= ids,
          "both key sessions are in the analyst profile's default session list (visible, not hidden)",
          listed=[r for r in rows or [] if r.get("id") in (first["session_id"], second["session_id"])])
    injected = manager_saw(ctx["m"], "KEYED-ANSWER")
    check("keyed", not injected and len(ctx["m"].main_requests()) == before,
          "nothing reached a manager conversation: no injection, no manager turn",
          manager_requests_during_trial=len(ctx["m"].main_requests()) - before)


async def trial_9(ctx) -> None:
    ack = ctx.get("task1") or {}
    check("9", False, "NOT PROVEN: clicking the link needs the desktop app; the isolated run has no desktop",
          link=ack.get("link"), session_id=ack.get("session_id"))


async def ask_status(ctx, mgr: Client, sid: str, task_id: str) -> dict:
    before = len(ctx["m"].main_requests())
    await say(mgr, sid, "CALL " + json.dumps({"action": "status", "task_id": task_id}))
    return await until(lambda: tool_result_of(ctx["m"], "status", before), 180, "status")


async def trial_timeout_status(ctx) -> None:
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "SLOW", "slow task")
        task_id = ack["task_id"]
        t0 = time.monotonic()
        wake = await until(lambda: manager_got(ctx["m"], f"Task {task_id}"), WAIT_LIMIT + 60, "timeout message")
        check("limit", "not cancelled" in wake, "past the wait limit the manager is told to check back",
              seconds=round(time.monotonic() - t0, 1), wake_message=wake)
        early = await ask_status(ctx, mgr, sid, task_id)
        row = early["tasks"][0]
        check("status", row["status"] == "waiting_timeout" and row.get("turn") in ("queued", "started")
              and "analyst" in early["targets"] and "default" not in early["targets"],
              "before the late answer: status reads the turn as still running, targets exclude the caller",
              status=early)
        await until(lambda: any("SLOW-DONE" in r[1] for r in analyst_db(
            "SELECT role, coalesce(content, '') FROM messages WHERE session_id = ?", (ack["session_id"],))),
            120, "late analyst answer stored")
        late = await ask_status(ctx, mgr, sid, task_id)
        row = late["tasks"][0]
        injected = [b for b in ctx["m"].main_requests() if b["messages"][-1].get("role") == "user"
                    and "SLOW-DONE" in text_of(b["messages"][-1])]
        check("late", row["status"] == "answered" and "SLOW-DONE" in (row.get("answer") or "")
              and row.get("delivered") is True and not injected,
              "after the wait limit, status fetched the answer that arrived later and handed it to the manager",
              status=late, analyst_session=ack["session_id"], seconds_since_start=round(time.monotonic() - t0, 1))
        again = await ask_status(ctx, mgr, sid, task_id)
        check("late", again["tasks"][0]["status"] == "answered" and "note" not in again["tasks"][0],
              "a second status keeps the answer and does not re-read the target", status=again)


def notice_log(task_id: str) -> dict:
    """The plugin's delivery record of the task's notices: delivered ones and those still kept."""
    task = plugin_task(task_id)
    return {"notices": task.get("notices") or [],
            "kept": [{k: e.get(k) for k in ("kind", "attempts")} for e in task.get("outbox") or []]}


async def call_tool(ctx, mgr: Client, sid: str, args: dict, action: str) -> dict:
    before = len(ctx["m"].main_requests())
    await say(mgr, sid, "CALL " + json.dumps(args))
    return await until(lambda: tool_result_of(ctx["m"], action, before), 180, f"{action} result")


async def trial_followup(ctx) -> None:
    """A follow_up while the task's turn runs is refused and points to correct; after the answer it is taken."""
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "HANGFU", "follow-up while running")
        task_id, asid = ack["task_id"], ack["session_id"]
        await until(lambda: any("SCENARIO:HANGFU" in text_of(m) for b in ctx["a"].main_requests()
                                for m in b["messages"]), 60, "analyst turn running")
        refused = await call_tool(ctx, mgr, sid, {"action": "follow_up", "task_id": task_id,
                                                  "text": "SCENARIO:FUEARLY extra"}, "follow_up")
        stored = plugin_task(task_id)
        early_users = [r for r in analyst_db("SELECT role, coalesce(content, '') FROM messages WHERE session_id = ?",
                                             (asid,)) if r[0] == "user" and "FUEARLY" in r[1]]
        check("followup", "error" in refused and "action=correct" in refused["error"]
              and len(stored.get("turns") or []) == 1 and stored.get("status") == "running" and not early_users,
              "follow_up during a running turn is refused, points to correct, and leaves the task state as it was",
              result=refused, task_turns=stored.get("turns"), task_status=stored.get("status"),
              analyst_rows_with_followup=len(early_users))
        answer = await answer_of(ctx, mgr, sid, task_id)
        taken = await call_tool(ctx, mgr, sid, {"action": "follow_up", "task_id": task_id,
                                                "text": "SCENARIO:FULATE extra"}, "follow_up")
        later = plugin_task(task_id)
        check("followup", "HANGFU-DONE" in answer and taken.get("accepted") is True
              and taken.get("session_id") == asid and len(later.get("turns") or []) == 2,
              "once the turn has ended, the same follow_up is taken in the same session as the task's second turn",
              first_answer=answer[:200], result=taken, task_turns=later.get("turns"))


async def trial_notice_retry(ctx) -> None:
    """Approval notices refused by a closed manager conversation are kept and handed over on the next tool call."""
    async with Client("default") as mgr:
        snap = await mgr.rpc("session.create", source="gui", request_id=uuid.uuid4().hex)
        sid = snap["session_id"]
        ack = await start_task(ctx, mgr, sid, "APPROVALLATE", "refused approval notice")
        task_id = ack["task_id"]
        # The start result is already in a model request; that request's reply ends the manager turn.
        await asyncio.sleep(2)
        # What the desktop sends when the conversation is closed.
        closed = await mgr.rpc("session.detach", session_id=sid, subscription_id=snap.get("subscription_id") or (
            await mgr.rpc("session.resume", session_id=sid, omit_messages=True))["subscription_id"],
                               end_reason="tui_close")
        # Until the task's result is kept too, no event of this task is left to retry the delivery.
        kept = await until(lambda: (lambda log: log if {e["kind"] for e in log["kept"]} >= {
            "approval", "approval_end", "result"} else None)(notice_log(task_id)),
            LATE_APPROVAL_DELAY + APPROVAL_TIMEOUT + 90, "approval, its ending and the result kept")
        missed = [manager_saw(ctx["m"], f"Task {task_id} (analyst) is waiting for an approval"),
                  manager_saw(ctx["m"], f"Task {task_id} (analyst): the approval for")]
        await mgr.rpc("session.resume", session_id=sid, omit_messages=True)
        await call_tool(ctx, mgr, sid, {"action": "status"}, "status")
        got = await until(lambda: manager_saw(ctx["m"], f"Task {task_id} (analyst): the approval for"), 120,
                          "kept notices delivered")
        after = await until(lambda: (lambda log: log if not log["kept"] else None)(notice_log(task_id)), 60,
                            "outbox emptied")
        check("retry", not any(missed) and all(not a["ok"] for e in kept["kept"] for a in e["attempts"])
              and all(n["trigger"] == "tool_call" for n in after["notices"]) and not after["kept"]
              and [n["kind"] for n in after["notices"]] == ["approval", "approval_end", "result"]
              and "expired unanswered" in got
              and manager_saw(ctx["m"], f"Task {task_id} (analyst) is waiting for an approval"),
              "the approval notice and its expiry, refused while the manager conversation was closed, were kept "
              "and handed over in order at the next profile_task call",
              session_close=closed, kept_before=kept, delivered_after=after, delivered_ending=got,
              seen_before_reopen=[bool(x) for x in missed],
              seen_after=bool(manager_saw(ctx["m"], f"Task {task_id} (analyst) is waiting for an approval")))


async def restart_gateway(ctx, how: str) -> dict:
    """Stop the isolated gateway (control socket ``force`` or SIGKILL) and start a new one."""
    from gateway.control_socket import identify_gateway
    old = ctx["proc"]
    t0 = time.monotonic()
    if how == "kill":
        # The child we start can attach to a host gateway it did not spawn; SIGKILL the pid that serves HH.
        host = int((identify_gateway(HH, timeout=5.0) or {}).get("pid") or 0)
        if not host:
            raise RuntimeError("no gateway answers on the isolated control socket")
        os.kill(host, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while alive(host) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        with contextlib.suppress(ProcessLookupError):
            os.kill(old.pid, signal.SIGKILL)
        old.wait(30)
        stopped = {"how": how, "old_pid": host, "old_alive": alive(host),
                   "stop_seconds": round(time.monotonic() - t0, 1)}
    else:
        host = int((identify_gateway(HH, timeout=5.0) or {}).get("pid") or 0)
        stop_by_socket(HH)
        old.wait(90)
        stopped = {"how": how, "old_pid": host, "old_alive": alive(host) if host else None,
                   "stop_seconds": round(time.monotonic() - t0, 1)}
    ctx["proc"] = start_gateway()
    async with Client("default"):
        pass
    stopped["new_pid"] = int((identify_gateway(HH, timeout=5.0) or {}).get("pid") or 0)
    return stopped


async def trial_restart(ctx) -> None:
    """Both restart kinds: at load the plugin finds the cut-off tasks and tells the manager conversation itself."""
    for how in ("socket", "kill"):
        hang, appr = f"HANG{how.upper()}", f"APPROVALRS{how.upper()}"
        async with Client("default") as mgr:
            sid = await manager_session(mgr)
            waiting = (await start_task(ctx, mgr, sid, hang, f"restart {how}"))["task_id"]
            await until(lambda: any(f"SCENARIO:{hang}" in text_of(m) for b in ctx["a"].main_requests()
                                    for m in b["messages"]), 60, "analyst turn running")
            approving = (await start_task(ctx, mgr, sid, appr, f"restart {how} with approval"))["task_id"]
            pending = await until(lambda: plugin_task(approving).get("pending_approval"), APPROVAL_TIMEOUT,
                                  "approval pending")
            hang_end = time.monotonic() + HANG_SECONDS + 5
        restarted = await restart_gateway(ctx, how)
        # No client opens the manager conversation and nothing calls the tool: the plugin acts at load.
        told = [await until(lambda t=t: manager_got(ctx["m"], f"Task {t} (analyst): the gateway restarted"),
                            120, "restart notice") for t in (waiting, approving)]
        recovered = {t: plugin_task(t) for t in (waiting, approving)}
        delivered = {t: notice_log(t) for t in (waiting, approving)}
        async with Client("default") as mgr:
            await mgr.rpc("session.resume", session_id=sid, omit_messages=True)
            check("restart", all(r.get("status") == "wait_lost" and r.get("waiter") is None for r in recovered.values())
                  and all(any(n["kind"] == "restart" and n["trigger"] == "load" for n in log["notices"])
                          and not log["kept"] for log in delivered.values())
                  and all(f"action=status task_id={t}" in m for t, m in zip((waiting, approving), told)),
                  f"gateway {how} restart: at load the plugin marked both cut-off tasks and the manager conversation "
                  f"got a notice for each, before any status call; notices refused earlier were kept and sent too",
                  restart=restarted, after_load={t: {k: r.get(k) for k in ("status", "waiter", "error")}
                                                 for t, r in recovered.items()},
                  delivery=delivered, notices=told)
            approval_after = recovered[approving]
            check("restart", pending.get("prompt_id") and not approval_after.get("pending_approval")
                  and not approval_after.get("settling_approval")
                  and (approval_after.get("last_approval") or {}).get("outcome") in ("interrupted", "cancelled")
                  and "did not run" in told[1],
                  f"gateway {how} restart with an approval open: the stale approval is cleared at load and the "
                  f"manager is told it ended with the restart",
                  pending_before=pending, last_approval=approval_after.get("last_approval"),
                  pending_after=approval_after.get("pending_approval"), notice=told[1])
            # Past the time the cut-off model call would have answered.
            await until(lambda: time.monotonic() >= hang_end, HANG_SECONDS + 60, "hang window over")
            again = await call_tool(ctx, mgr, sid, {"action": "status", "task_id": waiting}, "status")
            woke = manager_got(ctx["m"], f"{hang}-DONE")
            check("restart", again["tasks"][0]["status"] != "running" and not woke,
                  f"after the {how} restart nothing waits silently: a later status still reports, no late answer "
                  f"is injected", status=again, injected=woke)


def mcp_auth_notices(task_id: str) -> list[dict]:
    task = plugin_task(task_id)
    return [n for n in list(task.get("notices") or []) + list(task.get("outbox") or []) if n["kind"] == "mcp_auth"]


async def trial_mcp_auth(ctx) -> None:
    """A target profile's MCP server needing a new sign-in is reported to the manager, once per server and task."""
    async with Client("default") as mgr, Client("implementer") as viewer:
        sid = await manager_session(mgr)
        ack = await start_task(ctx, mgr, sid, "REAUTH", "mcp re-auth", profile="implementer")
        task_id, isid = ack["task_id"], ack["session_id"]
        await viewer.rpc("session.resume", session_id=isid, omit_messages=True)
        answer = await until(lambda: manager_got(ctx["m"], f"Answer from implementer for task {task_id}"), 120,
                             "re-auth task answer")
        status = (await viewer.rpc("session.info", session_id=isid)).get("mcp_servers")
        login = await until(lambda: manager_saw(ctx["m"], f"MCP server '{LOGIN_SERVER}' of the implementer"), 60,
                            "login-server notice")
        check("mcp_auth", f"hermes -p implementer mcp login {LOGIN_SERVER}" in login and f"Task {task_id}" in login,
              "hook (a): a remote implementer MCP server that refuses its sign-in (status failed) is reported to the "
              "manager conversation with the profile, the server and the login command",
              notice=login, implementer_mcp_status=status, answer=answer[:300])
        completes = [f.get("payload") for f in viewer.frames if f.get("type") == "tool.complete"]
        results = [(p or {}).get("result") for p in completes]
        parsed = [json.loads(x) if isinstance(x, str) and x.startswith("{") else x for x in results]
        refused = [x for x in parsed if isinstance(x, dict) and x.get("needs_reauth") is True]
        call = manager_saw(ctx["m"], f"MCP server '{CALL_SERVER}' of the implementer")
        served_401 = [line for line in Path(EVIDENCE["remote_mcp"]["inbound_log"]).read_text().splitlines()
                      if "injected_401_for" in line]
        # This Hermes folds a tools/call 401 into a generic MCPError (upstream #121285): no needs_reauth, so the
        # plugin maps mcp__linear__* to its connection and reads the 401 Hermes recorded (_http_rejection).
        # The viewer may resume after the first call finished; the second turn's results are checked below.
        check("mcp_auth", bool(served_401) and not refused and bool(call)
              and f"Task {task_id}" in call and "a tool call failed with HTTP 401" in call
              and f"hermes -p implementer mcp login {CALL_SERVER}" in call,
              "gap 1: a 401 on tools/call mid-task (generic MCPError, no needs_reauth) sends the manager one notice "
              "naming the server and the login command",
              server_answered_401=len(served_401), tool_results=results, needs_reauth_results=refused, notice=call)
        first = mcp_auth_notices(task_id)
        await call_tool(ctx, mgr, sid, {"action": "follow_up", "task_id": task_id,
                                        "text": "SCENARIO:REAUTHAGAIN try again"}, "follow_up")
        await until(lambda: manager_got(ctx["m"], "REAUTHAGAIN-RESULT"), 120, "second turn answer")
        await asyncio.sleep(3)
        again = mcp_auth_notices(task_id)
        texts = [n.get("text") or "" for n in again]
        expected = sorted([LOGIN_SERVER, CALL_SERVER])
        second_results = [(f.get("payload") or {}).get("result") for f in viewer.frames
                          if f.get("type") == "tool.complete"][len(results):]
        check("mcp_auth", len(first) == len(expected) and len(again) == len(expected) and len(second_results) == 2
              and not any("needs_reauth" in str(x) for x in second_results)
              and not plugin_task(task_id).get("outbox")
              and sorted(plugin_task(task_id).get("mcp_auth_noticed") or []) == expected,
              "one notice per server per task: the server state is read at the start and end of each turn, and a "
              "second turn of the task hitting the same servers (two more 401s, generic MCPError) adds none; every "
              "notice was taken by the manager conversation",
              notices_after_first_turn=[n["id"] for n in first], notices_after_second_turn=[n["id"] for n in again],
              kept=texts if plugin_task(task_id).get("outbox") else [], noticed=expected,
              second_turn_tool_results=second_results)
        # Gap 2: three failed calls opened the breaker. Hermes now refuses linear's calls while session.info
        # still shows it "connected"; a new task that calls nothing must still be told.
        rows = {r.get("name"): r for r in (await viewer.rpc("session.info", session_id=isid)).get("mcp_servers") or []}
        ack2 = await start_task(ctx, mgr, sid, "PLAIN", "after the breaker", profile="implementer")
        task2 = ack2["task_id"]
        await until(lambda: manager_got(ctx["m"], f"Answer from implementer for task {task2}"), 120, "plain answer")
        paused = await until(lambda: manager_saw(ctx["m"], f"Task {task2} (implementer): the MCP server "
                                                          f"'{CALL_SERVER}'"), 30, "set-aside notice")
        check("mcp_auth", (rows.get(CALL_SERVER) or {}).get("status") == "connected"
              and "paused it after" in paused and "HTTP 401" in paused
              and f"hermes -p implementer mcp login {CALL_SERVER}" in paused
              and not plugin_task(task2).get("outbox")
              and (plugin_task(task2).get("mcp_auth_noticed") or []).count(CALL_SERVER) == 1
              and len(mcp_auth_notices(task2)) == len(plugin_task(task2).get("mcp_auth_noticed") or []),
              "gap 2: a server Hermes set aside after repeated 401s (circuit breaker open, session.info still "
              "\"connected\", no needs_auth) is reported to the manager for a task that made no call to it",
              session_info_row=rows.get(CALL_SERVER), notice=paused, task=task2,
              noticed=plugin_task(task2).get("mcp_auth_noticed"))
    analyst_tasks = [t for t in json.loads(next((HH / "plugin-data").glob("*/state.json")).read_text())["tasks"].values()
                     if t.get("profile") == "analyst"]
    check("mcp_auth", not any(t.get("mcp_auth_noticed") for t in analyst_tasks),
          "a target whose MCP servers are all fine (analyst) gets no re-authentication notice",
          analyst_tasks=len(analyst_tasks))


async def trial_bad_target(ctx) -> None:
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        before = len(ctx["m"].main_requests())
        await say(mgr, sid, "CALL " + json.dumps({"action": "start", "profile": "default", "text": "x"}))
        result = await until(lambda: tool_result_of(ctx["m"], "start", before), 60, "refusal")
        check("targets", "error" in result and "analyst" in result["error"], "the caller itself is not a target",
              result=result)


TRIALS = {"1-2-3": trial_1_2_3, "4": trial_4, "5": trial_5, "6": trial_6, "7": trial_7, "8": trial_8,
          "9": trial_9, "limit": trial_timeout_status, "targets": trial_bad_target, "restart": trial_restart,
          "followup": trial_followup, "retry": trial_notice_retry, "mcp_auth": trial_mcp_auth, "keyed": trial_keyed,
          # Last: its 'always' relay permanently allows recursive delete in the analyst profile.
          "approval": trial_approval}


async def main(selected: list[str]) -> int:
    remote, remote_handles = start_remote_mcp()
    with FakeLLMServer(manager_model) as m, FakeLLMServer(analyst_model) as a, \
            contextlib.ExitStack() as cleanup:
        cleanup.callback(stop_remote_mcp, remote_handles)
        write_home(m.base_url, a.base_url, remote)
        ctx = {"m": m, "a": a, "proc": start_gateway()}
        try:
            async with Client("default"):
                pass
            served = json.loads((HH / "gateway_state.json").read_text()).get("served_profiles")
            log("gateway ready, served profiles:", served)
            EVIDENCE["served_profiles"] = served
            for name, trial in TRIALS.items():
                if selected and name not in selected:
                    continue
                log("=== trial", name)
                try:
                    await trial(ctx)
                except Exception as exc:  # noqa: BLE001 - a trial that cannot finish is a failure
                    check(name, False, f"trial did not finish: {exc.__class__.__name__}: {exc}")
        finally:
            stop_gateway(ctx["proc"])
            EVIDENCE["manager_requests"] = len(m.main_requests())
            EVIDENCE["manager_bodies_tail"] = [b["messages"][-3:] for b in m.main_requests()[-4:]]
            EVIDENCE["analyst_requests"] = len(a.main_requests())
            out = REPO / "e2e/evidence" / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(EVIDENCE, indent=2, default=str), encoding="utf-8")
            log("evidence:", out)
    return 0 if all(c["ok"] for c in EVIDENCE["checks"]) else 1


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate)
    sys.exit(asyncio.run(main(sys.argv[1:])))
