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
import time
import uuid
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
EVIDENCE: dict = {"base": str(HOME.parent), "canary": CANARY, "checks": []}
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


def manager_model(record: dict):
    messages = record["body"]["messages"]
    last = messages[-1]
    if last.get("role") == "tool":
        return Text("MANAGER-ACK")
    text = text_of(last)
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
    if tag == "APPROVAL":
        if not tool_results:
            return ToolCall("terminal", {"command": f"rm -rf {HOME}/approval-probe-dir"})
        return Text(f"APPROVAL-RESULT {text_of(tool_results[-1])[:400]}", reasoning=reasoning)
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


def write_home(manager_url: str, analyst_url: str) -> None:
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

    default = dict(common, model=model(manager_url), gateway={"multiplex_profiles": True}, plugins={
        "enabled": ["profile-tasks"],
        "entries": {"profile-tasks": {"allow_gateway_injection": True,
                                      "settings": {"wait_timeout_seconds": WAIT_LIMIT}}},
    })
    analyst = dict(common, model=model(analyst_url), display={"show_reasoning": True},
                   approvals={"mode": "smart", "timeout": 10},
                   mcp_servers={"atlassian": {"command": sys.executable, "args": [str(FIXTURE_MCP)],
                                              "env": {"PARITY_MCP_CANARY": CANARY, "PYTHONPATH": str(AGENT)},
                                              "connect_timeout": 60, "timeout": 60}})
    implementer = dict(common, model=model(analyst_url))
    homes = {"default": HH, "analyst": HH / "profiles/analyst", "implementer": HH / "profiles/implementer"}
    for name, cfg in (("default", default), ("analyst", analyst), ("implementer", implementer)):
        homes[name].mkdir(parents=True, exist_ok=True)
        (homes[name] / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        (homes[name] / ".env").write_text("OPENAI_API_KEY=sk-fake-e2e\n", encoding="utf-8")
    (HH / "plugins").mkdir(exist_ok=True)
    (HH / "plugins/profile-tasks").symlink_to(REPO / "profile-tasks")
    EVIDENCE["configs"] = {"default": default, "analyst": analyst}


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
        check("1", MCP_TOOL in ana_mcp and not mgr_mcp,
              "the analyst session is offered the atlassian MCP tool, the manager is offered no MCP tool",
              analyst_mcp_tools=ana_mcp, manager_mcp_tools=mgr_mcp, manager_direct_tools=mgr_tools,
              manager_catalog_has_profile_task="profile_task" in mgr_catalog)
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


async def restart_gateway(ctx, how: str) -> dict:
    """Stop the isolated gateway (control socket ``force`` or SIGKILL) and start a new one."""
    old = ctx["proc"]
    t0 = time.monotonic()
    if how == "kill":
        os.kill(old.pid, signal.SIGKILL)
        old.wait(30)
    else:
        stop_by_socket(HH)
        old.wait(90)
    stopped = {"how": how, "old_pid": old.pid, "old_alive": alive(old.pid),
               "stop_seconds": round(time.monotonic() - t0, 1)}
    ctx["proc"] = start_gateway()
    async with Client("default"):
        pass
    stopped["new_pid"] = ctx["proc"].pid
    return stopped


async def trial_restart(ctx) -> None:
    for how in ("socket", "kill"):
        tag = f"HANG{how.upper()}"
        async with Client("default") as mgr:
            sid = await manager_session(mgr)
            ack = await start_task(ctx, mgr, sid, tag, f"restart {how}")
            task_id = ack["task_id"]
            await until(lambda: any(f"SCENARIO:{tag}" in text_of(m) for b in ctx["a"].main_requests()
                                    for m in b["messages"]), 60, "analyst turn running")
            hang_end = time.monotonic() + HANG_SECONDS + 5
        restarted = await restart_gateway(ctx, how)
        async with Client("default") as mgr:
            sid = await manager_session(mgr)
            status = await ask_status(ctx, mgr, sid, task_id)
            row = status["tasks"][0]
            note = row.get("note") or ""
            check("restart", row["status"] != "running" and ("restarted" in note or "lost its connection" in note),
                  f"gateway {how} stop while the task runs: status says the wait was lost and what happened",
                  restart=restarted, status=status, analyst_session=ack["session_id"])
            # Past the time the cut-off model call would have answered.
            await until(lambda: time.monotonic() >= hang_end, HANG_SECONDS + 60, "hang window over")
            again = await ask_status(ctx, mgr, sid, task_id)
            woke = manager_got(ctx["m"], f"{tag}-DONE")
            check("restart", again["tasks"][0]["status"] != "running" and not woke,
                  f"after the {how} restart nothing waits silently: a later status still reports, no late answer "
                  f"is injected",
                  status=again, injected=woke)


async def trial_bad_target(ctx) -> None:
    async with Client("default") as mgr:
        sid = await manager_session(mgr)
        before = len(ctx["m"].main_requests())
        await say(mgr, sid, "CALL " + json.dumps({"action": "start", "profile": "default", "text": "x"}))
        result = await until(lambda: tool_result_of(ctx["m"], "start", before), 60, "refusal")
        check("targets", "error" in result and "analyst" in result["error"], "the caller itself is not a target",
              result=result)


TRIALS = {"1-2-3": trial_1_2_3, "4": trial_4, "5": trial_5, "6": trial_6, "7": trial_7, "8": trial_8,
          "9": trial_9, "limit": trial_timeout_status, "targets": trial_bad_target, "restart": trial_restart}


async def main(selected: list[str]) -> int:
    with FakeLLMServer(manager_model) as m, FakeLLMServer(analyst_model) as a:
        write_home(m.base_url, a.base_url)
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
