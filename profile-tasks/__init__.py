"""profile-tasks: the manager hands a task to another profile served by the same Hermes gateway.

Each task is one session created under the target profile the way the desktop creates one
(``session.create`` with ``source="gui"``), then the text is submitted with ``prompt.submit``.
The target runs with its own config, MCP servers and tokens; only its final text comes back,
injected once into the calling conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOOL_NAME = "profile_task"
TOOLSET = "profile_tasks"
STATE_KEY = "tasks"
DEFAULT_WAIT_SECONDS = 1800
ACK_TIMEOUT_SECONDS = 120
# After the wait limit the waiter keeps watching the target turn this long, only to relay approvals.
APPROVAL_WATCH_SECONDS = 24 * 3600
RELAY_RULE = "profile_task_relay"
# A process that loads the plugin but never hosts the gateway (CLI, plugin listing) stops waiting after this.
LOAD_FLUSH_WAIT_SECONDS = 300
APPROVAL_OUTCOMES = ("denied", "timeout", "cancelled", "notify_failed", "blocked")
# Target frames the waiter hands to the plugin: approvals, the tool result that shows how one ended, turn ends.
WATCHED_FRAMES = ("approval.request", "approval.settled", "tool.complete", "message.complete")
RPC_METHODS = ("session.create", "session.resume", "prompt.submit", "prompt.receipt", "session.steer",
               "approval.respond")
_REQUIRED_FIELDS = {
    "session.create": {"source", "request_id", "title"},
    "session.resume": {"session_id"},
    "prompt.submit": {"session_id", "text", "submission_id"},
    "prompt.receipt": {"session_id", "admission_id", "include_result"},
    "session.steer": {"session_id", "execution_generation", "text"},
    "approval.respond": {"session_id", "choice"},
}
_STATE_LOCK = threading.Lock()
WAITING = ("starting", "running")
REFRESHABLE = ("starting", "running", "waiting_timeout", "wait_lost")

TARGET_GUIDANCE = (
    "Nobody watches this session live and nobody can answer a clarify question here. "
    "If you need information, ask for it in your final answer."
)


def check_internals() -> list[str]:
    """Names of the Hermes internals this plugin uses that the running Hermes no longer has."""
    missing: list[str] = []

    def need(module: str, names: tuple[str, ...]) -> Any:
        try:
            mod = __import__(module, fromlist=list(names))
        except Exception as exc:  # noqa: BLE001 - any import failure means the internal is gone
            missing.append(f"{module} ({exc.__class__.__name__}: {exc})")
            return None
        for name in names:
            if not hasattr(mod, name):
                missing.append(f"{module}.{name}")
        return mod

    client = need("hermes_cli.gateway_client",
                  ("GATEWAY_WS_PROTOCOL", "GatewayClient", "GatewayClientError", "_session_ticket", "gateway_ws_target"))
    runtime = need("hermes_cli.gateway_runtime", ("ensure_gateway_runtime",))
    need("hermes_cli.gateway_multiplex_served", ("recorded_served_profiles",))
    need("hermes_cli.profiles", ("get_profile_dir",))
    need("hermes_constants", ("get_hermes_home",))
    need("gateway.session_context", ("bound_session_key",))
    need("tools.approval", ("request_tool_approval", "list_gateway_approvals", "withdraw_gateway_approval"))
    need("tools.approval_context", ("get_current_session_key",))
    need("agent.i18n", ("supported_languages", "t"))
    plugins = need("hermes_cli.plugins", ("PluginManager",))
    if plugins is not None and not hasattr(getattr(plugins, "PluginManager", None), "has_gateway_message_injector"):
        missing.append("hermes_cli.plugins.PluginManager.has_gateway_message_injector")
    need("websockets.asyncio.client", ("connect",))
    if client is not None and hasattr(client, "_session_ticket"):
        params = list(inspect.signature(client._session_ticket).parameters)
        if params[:2] != ["home", "endpoint"]:
            missing.append(f"hermes_cli.gateway_client._session_ticket(home, endpoint) (found {params})")
    if client is not None and hasattr(client, "GatewayClient") and not hasattr(client.GatewayClient, "rpc"):
        missing.append("hermes_cli.gateway_client.GatewayClient.rpc")
    if runtime is not None and hasattr(runtime, "ensure_gateway_runtime"):
        params = list(inspect.signature(runtime.ensure_gateway_runtime).parameters)
        if params[:1] != ["profile_home"]:
            missing.append(f"hermes_cli.gateway_runtime.ensure_gateway_runtime(profile_home) (found {params})")
    try:
        import tui_gateway.contracts  # noqa: F401 - registers the contracts
        from tui_gateway.contracts.registry import CANONICAL_METHODS
    except Exception as exc:  # noqa: BLE001
        missing.append(f"tui_gateway.contracts.registry.CANONICAL_METHODS ({exc.__class__.__name__}: {exc})")
    else:
        for method, fields in _REQUIRED_FIELDS.items():
            contract = CANONICAL_METHODS.get(method)
            if contract is None:
                missing.append(f"gateway method {method}")
                continue
            absent = fields - set(contract.params.model_fields)
            if absent:
                missing.append(f"gateway method {method} fields {sorted(absent)}")
    return missing


def _approval_outcome(payload: dict) -> tuple[str, str]:
    """How the gated tool call ended: ("ran", "") or (outcome, user_summary) read from its blocked result."""
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    if not isinstance(result, dict) or result.get("status") != "blocked":
        return "ran", ""
    summary = str(result.get("user_summary") or "")
    return _summary_outcome(summary), summary


def _summary_outcome(summary: str) -> str:
    """Match a localized approval summary (tools/approval.py _user_summary) back to its outcome key."""
    from agent.i18n import supported_languages, t
    for lang in supported_languages():
        for outcome in APPROVAL_OUTCOMES:
            template = t(f"approval.summary.{outcome}", lang=lang)
            pattern = re.escape(template).replace(r"\{noun\}", ".+?").replace(r"\{window\}", ".+?")
            if summary and re.fullmatch(pattern, summary):
                return outcome
    return "not_run"


# ---- state ------------------------------------------------------------------------------------


class TaskStore:
    """Tasks kept in the plugin's durable state, one dict per task id."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def all(self) -> dict[str, dict]:
        with _STATE_LOCK:
            return dict(self._state.get(STATE_KEY, {}) or {})

    def get(self, task_id: str) -> dict | None:
        return self.all().get(task_id)

    def update(self, task_id: str, /, **fields: Any) -> dict:
        with _STATE_LOCK:
            tasks = dict(self._state.get(STATE_KEY, {}) or {})
            task = dict(tasks.get(task_id, {}))
            task.update(fields, updated_at=time.time())
            tasks[task_id] = task
            self._state.set(STATE_KEY, tasks)
            return task


# ---- gateway client ---------------------------------------------------------------------------


class _Connection:
    """One websocket connection to a profile's gateway, opened with a ticket bound to that profile."""

    def __init__(self, home: Path, on_frame: Any = None) -> None:
        self.home = home
        self.on_frame = on_frame
        self._stack = None
        self.client = None

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        from hermes_cli.gateway_client import (GATEWAY_WS_PROTOCOL, GatewayClient, GatewayClientError,
                                               _session_ticket, gateway_ws_target)
        from hermes_cli.gateway_runtime import ensure_gateway_runtime
        from websockets.asyncio.client import connect

        ready = await asyncio.to_thread(ensure_gateway_runtime, self.home)
        if ready.state != "ready" or ready.endpoint is None:
            raise GatewayClientError(f"gateway_{ready.state}:{ready.reason_code or 'not_ready'}")
        ticket = await asyncio.to_thread(_session_ticket, self.home, ready.endpoint)
        url, protocols = gateway_ws_target(ready.endpoint, ticket)
        self._stack = AsyncExitStack()
        # Loopback dial: never through HTTP(S)_PROXY.
        ws = await self._stack.enter_async_context(
            connect(url, subprotocols=protocols, open_timeout=10, max_size=8 * 1024 * 1024, proxy=None))
        if ws.subprotocol != GATEWAY_WS_PROTOCOL:
            await self._stack.aclose()
            raise GatewayClientError("gateway_protocol_mismatch")
        self.client = await self._stack.enter_async_context(GatewayClient(ws))
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def rpc(self, method: str, **params: Any) -> Any:
        return await self.client.rpc(method, **params)

    async def wait_terminal(self, session_id: str, admission_id: str, deadline: float) -> dict:
        """The admission's receipt once terminal; raises TimeoutError at *deadline* (monotonic)."""
        backoff = 0.25
        while True:
            receipt = await self.rpc("prompt.receipt", session_id=session_id, admission_id=admission_id,
                                     include_result=True)
            if receipt["status"] in ("terminal", "unknown"):
                return receipt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                async with asyncio.timeout(min(backoff, remaining)):
                    await self._completion(admission_id)
            except TimeoutError:
                backoff = min(backoff * 2, 5.0)

    async def _completion(self, admission_id: str) -> None:
        while True:
            event = await self.client.events.get()
            if isinstance(event, Exception):
                raise event
            frame = event.get("params") or {}
            if self.on_frame is not None and frame.get("type") in WATCHED_FRAMES:
                try:
                    self.on_frame(frame)
                except Exception:  # noqa: BLE001 - a relay failure must not end the wait
                    logger.warning("profile-tasks: target frame handling failed", exc_info=True)
            if frame.get("type") == "message.complete" and (frame.get("payload") or {}).get("admission_id") == admission_id:
                return


def _process_token(pid: int) -> str:
    """pid plus its start time (Linux /proc), so a reused pid does not pass for the waiting process."""
    try:
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        start = ""
    return f"{pid}:{start}"


def _waiter_alive(token: str | None) -> bool:
    """Whether the process that recorded *token* is still the one running under that pid."""
    if not token:
        return False
    pid = int(token.split(":", 1)[0])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        # A killed waiter stays a zombie until its parent reaps it; it no longer waits.
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] in ("Z", "X"):
            return False
    except (OSError, IndexError):
        return False
    return _process_token(pid) == token


def _final_text(receipt: dict) -> tuple[bool, str]:
    """(succeeded, text) from a terminal receipt."""
    result = receipt.get("result") or {}
    if isinstance(result.get("result"), dict):
        result = result["result"]
    text = result.get("final_response") or ""
    failed = (receipt.get("status") == "unknown" or receipt.get("outcome") not in (None, "completed")
              or result.get("failed") or result.get("interrupted") or result.get("error"))
    if failed:
        cause = result.get("error") or receipt.get("outcome") or receipt.get("status")
        return False, f"{cause}" + (f"\n\nPartial answer:\n{text}" if text else "")
    return True, text


# ---- plugin -----------------------------------------------------------------------------------


class ProfileTasks:
    def __init__(self, ctx: Any, missing: list[str]) -> None:
        self.ctx = ctx
        self.missing = missing
        self.store = TaskStore(ctx.state)
        # prompt_id -> {"manager_key", "rule", "choice"} while a relayed card is open in the manager turn.
        self._relays: dict[str, dict] = {}
        self._relay_lock = threading.Lock()
        self._outbox_lock = threading.Lock()
        self._send_lock = threading.Lock()

    def _gateway_host(self) -> bool:
        """True once this process runs a gateway that accepts plugin messages."""
        return bool(getattr(getattr(self.ctx, "_manager", None), "has_gateway_message_injector", False))

    # settings
    def _wait_seconds(self) -> float:
        value = self.ctx.get_config("wait_timeout_seconds", DEFAULT_WAIT_SECONDS)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULT_WAIT_SECONDS)

    # profiles
    def _caller_profile(self) -> str:
        from hermes_cli.gateway_multiplex_served import recorded_served_profiles
        from hermes_cli.profiles import get_profile_dir
        from hermes_constants import get_hermes_home
        home = get_hermes_home().resolve()
        for name in recorded_served_profiles() or ["default"]:
            try:
                if get_profile_dir(name).resolve() == home:
                    return name
            except ValueError:
                continue
        return self.ctx.profile_name

    def targets(self) -> list[str]:
        from hermes_cli.gateway_multiplex_served import recorded_served_profiles
        caller = self._caller_profile()
        return [p for p in (recorded_served_profiles() or []) if p != caller]

    @staticmethod
    def _home(profile: str) -> Path:
        from hermes_cli.profiles import get_profile_dir
        return get_profile_dir(profile)

    # tool entry
    def handle(self, args: dict, **_kw: Any) -> str:
        from tools.registry import tool_error, tool_result
        if self.missing:
            return tool_error("profile_task is unavailable: this Hermes no longer has the internals it "
                              "depends on: " + "; ".join(self.missing))
        action = (args.get("action") or "").strip()
        if self._gateway_host():
            self._flush_outbox("tool_call")
        try:
            if action == "start":
                return tool_result(self.start(args.get("profile") or "", args.get("text") or "", args.get("title")))
            if action == "follow_up":
                return tool_result(self.follow_up(args.get("task_id") or "", args.get("text") or ""))
            if action == "correct":
                return tool_result(self.correct(args.get("task_id") or "", args.get("text") or ""))
            if action == "status":
                return tool_result(self.status(args.get("task_id")))
            if action == "approval":
                return tool_result(self.approval(args.get("task_id") or ""))
            return tool_error("action must be one of: start, follow_up, correct, status, approval")
        except ValueError as exc:
            return tool_error(str(exc))

    # actions
    def start(self, profile: str, text: str, title: str | None) -> dict:
        targets = self.targets()
        if profile not in targets:
            raise ValueError(f"profile must be one of {targets}")
        if not text.strip():
            raise ValueError("text is required")
        from gateway.session_context import bound_session_key
        caller_key = bound_session_key()
        if not caller_key:
            raise ValueError("no calling conversation to deliver the answer to")
        task_id = "t-" + uuid.uuid4().hex[:8]
        label = f"Task {task_id}" + (f": {title.strip()[:80]}" if title and title.strip() else "")
        self.store.update(task_id, task_id=task_id, profile=profile, caller_session_key=caller_key,
                          title=label, created_at=time.time(), status="starting", session_id=None, turns=[],
                          waiter=_process_token(os.getpid()))
        return self._send(task_id, text, create=True)

    def follow_up(self, task_id: str, text: str) -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        if not task.get("session_id"):
            raise ValueError(f"task {task_id} has no session yet")
        if not text.strip():
            raise ValueError("text is required")
        from gateway.session_context import bound_session_key
        with self._send_lock:
            running = self._running_turn(task)
            if running:
                raise ValueError(
                    f"task {task_id} has a turn running in {task['profile']} ({running}). A follow_up now would "
                    f"start a second turn on the same task. Use action=correct to change the running turn, or "
                    f"send the follow_up after its answer arrives.")
            caller_key = bound_session_key() or task["caller_session_key"]
            self.store.update(task_id, caller_session_key=caller_key, status="starting",
                              waiter=_process_token(os.getpid()))
        return self._send(task_id, text, create=False)

    def _running_turn(self, task: dict) -> str | None:
        """Why the task's turn counts as running (a short reason), or None when a follow_up may start."""
        if task.get("status") == "starting" and _waiter_alive(task.get("waiter")):
            return "being submitted"
        if not task.get("admission_id"):
            return None
        answer = self._run_async(self._read_receipt_ok(task))
        if not answer.get("ok"):
            return f"its state could not be read: {answer.get('error')}"
        state = answer["receipt"].get("status")
        return None if state in ("terminal", "unknown") else f"turn {state}"

    async def _read_receipt_ok(self, task: dict) -> dict:
        return {"ok": True, "receipt": await self._read_receipt(task)}

    def _send(self, task_id: str, text: str, *, create: bool) -> dict:
        """Submit *text* on a worker thread; return once the target accepted it (or failed to)."""
        accepted: dict = {}
        ready = threading.Event()
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(self._run_send, task_id, text, create, accepted, ready),
                                  name=f"profile-task-{task_id}", daemon=True)
        thread.start()
        if not ready.wait(ACK_TIMEOUT_SECONDS):
            return {"task_id": task_id, "accepted": False,
                    "error": f"the target gateway did not accept the task within {ACK_TIMEOUT_SECONDS}s; "
                             "check later with action=status"}
        if "error" in accepted:
            self.store.update(task_id, status="failed", error=accepted["error"])
            return {"task_id": task_id, "accepted": False, "error": accepted["error"]}
        task = self.store.get(task_id)
        return {
            "task_id": task_id, "accepted": True, "profile": task["profile"], "session_id": task["session_id"],
            "session_title": task["title"], "link": f"hermes://open/{task['session_id']}",
            "note": (f"The {task['profile']} profile is working on it. Finish your turn now without waiting: "
                     "its final answer will arrive in this conversation as a new message."),
        }

    def _run_send(self, task_id: str, text: str, create: bool, accepted: dict, ready: threading.Event) -> None:
        try:
            asyncio.run(self._send_and_wait(task_id, text, create, accepted, ready))
        except Exception as exc:  # noqa: BLE001 - every failure is reported to the manager
            logger.warning("profile-tasks: task %s failed", task_id, exc_info=True)
            if not ready.is_set():
                accepted["error"] = f"{exc.__class__.__name__}: {exc}"
                ready.set()
            else:
                error = f"{exc.__class__.__name__}: {exc}"
                lost = self._drop_dead_approval(task_id, "the plugin lost its connection to the target session")
                self._deliver(task_id, f"Task {task_id}: the wait for the answer was cut off ({error}). The work in "
                                       f"the target session may still finish. Check with profile_task action=status "
                                       f"task_id={task_id}.{lost}", status="wait_lost", error=error, kind="wait_lost")

    async def _send_and_wait(self, task_id: str, text: str, create: bool, accepted: dict,
                             ready: threading.Event) -> None:
        task = self.store.get(task_id)
        async with _Connection(self._home(task["profile"]),
                               on_frame=lambda frame: self._on_target_frame(task_id, frame)) as conn:
            if create:
                snapshot = await conn.rpc("session.create", source="gui", request_id=task_id, title=task["title"])
                session_id = snapshot["session_id"]
                self.store.update(task_id, session_id=session_id)
                payload = f"{text}\n\n[{TARGET_GUIDANCE}]"
            else:
                session_id = task["session_id"]
                await conn.rpc("session.resume", session_id=session_id, omit_messages=True)
                payload = text
            submission_id = uuid.uuid4().hex
            receipt = await conn.rpc("prompt.submit", session_id=session_id, text=payload,
                                     submission_id=submission_id)
            admission_id = receipt["admission_id"]
            turns = list(self.store.get(task_id).get("turns") or []) + [admission_id]
            self.store.update(task_id, status="running", admission_id=admission_id, turns=turns,
                              waiter=_process_token(os.getpid()))
            ready.set()
            deadline = time.monotonic() + self._wait_seconds()
            try:
                final = await conn.wait_terminal(session_id, admission_id, deadline)
            except TimeoutError:
                minutes = round(self._wait_seconds() / 60, 1)
                self._deliver(task_id, (
                    f"Task {task_id} ({task['profile']}): no answer within {minutes} minutes. The work is not "
                    f"cancelled. Check back later with profile_task action=status task_id={task_id}."),
                    status="waiting_timeout", kind="wait_limit")
                # Keep relaying approvals until the turn ends; status reads the answer itself.
                with contextlib.suppress(TimeoutError):
                    await conn.wait_terminal(session_id, admission_id, time.monotonic() + APPROVAL_WATCH_SECONDS)
                return
        ok, body = _final_text(final)
        if ok:
            message = f"Answer from {task['profile']} for task {task_id} (session {session_id}):\n\n{body}"
            self._deliver(task_id, message, status="answered", last_answer=body)
        else:
            message = (f"Task {task_id} ({task['profile']}) did not complete: {body}\n"
                       f"The {task['profile']} session {session_id} still exists.")
            self._deliver(task_id, message, status="failed", error=body)

    # approvals raised in a target session
    def _on_target_frame(self, task_id: str, frame: dict) -> None:
        task = self.store.get(task_id) or {}
        if frame.get("session_id") not in (None, task.get("session_id")):
            return
        payload = frame.get("payload") or {}
        kind = frame.get("type")
        if kind == "approval.request" and payload.get("prompt_id"):
            self._approval_raised(task_id, payload)
        elif kind == "approval.settled" and payload.get("prompt_id"):
            self._approval_settled(task_id, payload)
        # On expiry the gated tool's result can arrive before approval.settled.
        elif kind == "tool.complete" and (task.get("settling_approval") or task.get("pending_approval")):
            self._approval_ended(task_id, _approval_outcome(payload))
        elif kind == "message.complete" and (task.get("settling_approval") or task.get("pending_approval")):
            self._approval_ended(task_id, ("interrupted", ""))

    def _approval_raised(self, task_id: str, payload: dict) -> None:
        task = self.store.get(task_id) or {}
        profile, session_id = task.get("profile"), task.get("session_id")
        timeout_s = float(payload.get("timeout_s") or 0)
        raised = time.time()
        pending = {"prompt_id": payload["prompt_id"], "execution_generation": payload.get("execution_generation"),
                   "command": payload.get("command") or "", "description": payload.get("description") or "",
                   "choices": list(payload.get("choices") or ["once", "deny"]), "timeout_s": timeout_s,
                   "raised_at": raised, "expires_at": raised + timeout_s}
        self.store.update(task_id, pending_approval=pending)
        expires = time.strftime("%H:%M:%S", time.localtime(pending["expires_at"]))
        self._notify(task_id, (
            f"Task {task_id} ({profile}) is waiting for an approval in session {session_id} "
            f"(hermes://open/{session_id}). It wants to run: {pending['command']} ({pending['description']}). "
            f"The request expires in {round(timeout_s)} s, at {expires}; unanswered, the command does not run. "
            f"To answer it here, call profile_task action=approval task_id={task_id}: the approve/deny prompt "
            f"opens in this conversation and the answer goes to the {profile} session. It can also be answered "
            f"in the {profile} session itself."), "approval")

    def _approval_settled(self, task_id: str, payload: dict) -> None:
        task = self.store.get(task_id) or {}
        pending = task.get("pending_approval") or {}
        prompt_id = payload["prompt_id"]
        if pending.get("prompt_id") != prompt_id:
            return
        with self._relay_lock:
            relay = self._relays.get(prompt_id)
        forwarded = bool(relay and relay.get("forwarded")) or bool(pending.get("relayed"))
        # The settled frame names no outcome; the gated tool's result that follows it does.
        self.store.update(task_id, pending_approval=None,
                          settling_approval={**pending, "settled_at": time.time(), "relayed": forwarded})
        if relay is not None and not forwarded:
            self._withdraw_manager_card(relay, "the request in the target session ended")

    def _approval_ended(self, task_id: str, outcome: tuple[str, str]) -> None:
        """Record how an approval really ended and tell the calling conversation, unless it answered it itself."""
        task = self.store.get(task_id) or {}
        approval = task.get("settling_approval") or task.get("pending_approval")
        if not approval:
            return
        name, summary = outcome
        settled = approval.get("settled_at") or time.time()
        record = {**approval, "outcome": name, "user_summary": summary,
                  "settled_after_s": round(settled - approval["raised_at"], 1)}
        self.store.update(task_id, pending_approval=None, settling_approval=None, last_approval=record)
        if approval.get("relayed"):
            return
        profile, session_id = task.get("profile"), task.get("session_id")
        what = {
            "ran": f"was approved in the {profile} session; the command ran",
            "denied": f"was denied in the {profile} session; the command did not run",
            "timeout": (f"expired unanswered after {record['settled_after_s']} s (the {profile} profile's "
                        f"approvals.timeout is {round(approval['timeout_s'])} s); the command did not run"),
            "cancelled": "was withdrawn before anyone answered (the turn was interrupted or corrected); the "
                         "command did not run",
            "interrupted": f"ended with the {profile} turn before the command reported back; it did not run",
        }.get(name, f"ended without running the command ({summary or name})")
        self._notify(task_id, (f"Task {task_id} ({profile}): the approval for `{approval.get('command')}` {what}. "
                               f"Session {session_id} (hermes://open/{session_id})."), "approval_end")

    @staticmethod
    def _withdraw_manager_card(relay: dict, cause: str) -> None:
        from tools.approval import list_gateway_approvals, withdraw_gateway_approval
        for entry in list_gateway_approvals(relay["manager_key"]):
            if entry.get("pattern_key") == relay["rule"] and entry.get("request_id"):
                withdraw_gateway_approval(relay["manager_key"], entry["request_id"], cause)

    def on_approval_response(self, **kw: Any) -> None:
        """post_approval_response hook: records the choice made on a relayed card."""
        key = str(kw.get("pattern_key") or "")
        prefix = f"plugin_rule:{RELAY_RULE}:"
        if key.startswith(prefix):
            with self._relay_lock:
                relay = self._relays.get(key[len(prefix):])
                if relay is not None:
                    relay["choice"] = kw.get("choice")

    def approval(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        pending = task.get("pending_approval")
        if not pending:
            last = task.get("last_approval")
            raise ValueError(f"task {task_id} has no pending approval"
                             + (f" (the last one ended: {last.get('outcome')})" if last else ""))
        if time.time() >= pending["expires_at"]:
            raise ValueError(f"the approval of task {task_id} has expired")
        from tools.approval import request_tool_approval
        from tools.approval_context import get_current_session_key
        profile, prompt_id = task["profile"], pending["prompt_id"]
        rule_key = f"{RELAY_RULE}:{prompt_id}"
        relay = {"manager_key": get_current_session_key(), "rule": f"plugin_rule:{rule_key}", "choice": None}
        with self._relay_lock:
            self._relays[prompt_id] = relay
        expires = time.strftime("%H:%M:%S", time.localtime(pending["expires_at"]))
        reason = (f"the {profile} task {task_id} asks to run: {pending['command']} ({pending['description']}). "
                  f"Your answer is passed to the {profile} session {task['session_id']}, where the request "
                  f"expires at {expires}. 'always' permanently allows that kind of command in the {profile} "
                  f"profile (its command_allowlist), and leaves one inert rule for this prompt in the manager "
                  f"profile's config")
        try:
            result = request_tool_approval(TOOL_NAME, reason, rule_key=rule_key)
            choice = relay["choice"]
            if choice not in ("once", "session", "always", "deny"):
                note = ("No answer was given here" if choice in ("timeout", "cancelled") else
                        "This conversation's approvals are bypassed (yolo or approvals.mode off), so nobody "
                        "answered and nothing was passed on")
                still = (self.store.get(task_id) or {}).get("pending_approval")
                return {"task_id": task_id, "forwarded": False, "manager_choice": choice,
                        "approved_here": bool(result.get("approved")),
                        "note": f"{note}. The request in the {profile} session is "
                                + ("still pending until " + expires if still else "no longer pending") + "."}
            if choice != "deny" and choice not in pending["choices"]:
                choice = "once"
            relay["forwarded"] = True
            self.store.update(task_id, pending_approval={**pending, "relayed": True, "relayed_choice": choice})
            answer = self._run_async(self._respond(task, pending, choice))
        finally:
            with self._relay_lock:
                self._relays.pop(prompt_id, None)
        return {"task_id": task_id, "forwarded": True, "choice": choice, "target_answer": answer,
                "note": (f"Your {choice} was passed to the {profile} session; its final answer will arrive here."
                         if answer.get("ok") else f"The {profile} session did not take the answer: {answer}")}

    async def _respond(self, task: dict, pending: dict, choice: str) -> dict:
        async with _Connection(self._home(task["profile"])) as conn:
            await conn.rpc("session.resume", session_id=task["session_id"], omit_messages=True)
            try:
                result = await conn.rpc("approval.respond", session_id=task["session_id"],
                                        execution_generation=pending["execution_generation"],
                                        prompt_id=pending["prompt_id"], choice=choice)
            except Exception as exc:  # noqa: BLE001 - returned to the manager as the outcome
                return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return {"ok": True, "result": result}

    @staticmethod
    def _run_async(coro: Any) -> Any:
        box: dict = {}

        def run() -> None:
            try:
                box["value"] = asyncio.run(coro)
            except Exception as exc:  # noqa: BLE001
                box["value"] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

        thread = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True)
        thread.start()
        thread.join(ACK_TIMEOUT_SECONDS)
        return box.get("value", {"ok": False, "error": "timed out"})

    # notices to the calling conversation: kept in the task until that conversation takes them
    def _notify(self, task_id: str, message: str, kind: str, trigger: str = "event") -> bool:
        """Queue *message* for the task's calling conversation and try to hand over the queue in order."""
        self._queue_only(task_id, message, kind)
        return self._flush_task(task_id, trigger)

    def _flush_outbox(self, trigger: str) -> None:
        for task_id, task in self.store.all().items():
            if task.get("outbox"):
                self._flush_task(task_id, trigger)

    def _flush_task(self, task_id: str, trigger: str) -> bool:
        """Inject the task's queued notices oldest first; stop at the first refusal. True when none is left."""
        with self._outbox_lock:
            task = self.store.get(task_id) or {}
            outbox = list(task.get("outbox") or [])
            sent = list(task.get("notices") or [])
            fields: dict = {}
            while outbox:
                entry = dict(outbox[0])
                try:
                    ok = bool(self.ctx.inject_message(entry["text"], session_key=task.get("caller_session_key")))
                except Exception:  # noqa: BLE001 - a raising injection counts as a refusal
                    logger.warning("profile-tasks: injection for task %s raised", task_id, exc_info=True)
                    ok = False
                entry["attempts"] = list(entry["attempts"]) + [{"at": time.time(), "trigger": trigger, "ok": ok}]
                if not ok:
                    outbox[0] = entry
                    logger.warning("profile-tasks: the calling conversation refused a %s notice of task %s; kept "
                                   "for the next try", entry["kind"], task_id)
                    break
                outbox.pop(0)
                sent.append({k: entry[k] for k in ("id", "kind", "queued_at", "attempts")}
                            | {"delivered_at": time.time(), "trigger": trigger})
                if entry["kind"] == "result":
                    fields.update(delivered=True, delivered_by="injection")
            self.store.update(task_id, outbox=outbox, notices=sent, **fields)
            return not outbox

    def _drop_notices(self, task_id: str, kind: str) -> None:
        with self._outbox_lock:
            task = self.store.get(task_id) or {}
            self.store.update(task_id, outbox=[e for e in task.get("outbox") or [] if e["kind"] != kind])

    def _deliver(self, task_id: str, message: str, *, status: str, kind: str = "result", **fields: Any) -> None:
        self.store.update(task_id, status=status, **({"delivered": False} if kind == "result" else {}), **fields)
        self._notify(task_id, message, kind)

    def _drop_dead_approval(self, task_id: str, cause: str) -> str:
        """Clear an approval nobody can watch any more; return a sentence for the notice, or ''."""
        task = self.store.get(task_id) or {}
        approval = task.get("pending_approval") or task.get("settling_approval")
        if not approval:
            return ""
        self.store.update(task_id, pending_approval=None, settling_approval=None,
                          last_approval={**approval, "outcome": "watch_lost", "user_summary": cause,
                                         "settled_at": time.time()})
        return (f" Its approval request for `{approval.get('command')}` can no longer be followed or answered from "
                f"here ({cause}); answer it in the {task.get('profile')} session if it is still open.")

    # gateway restart: found at load, told to each calling conversation
    def recover_after_restart(self) -> list[str]:
        """Close out tasks whose waiting process is gone; queue one notice each. Returns their ids."""
        found = []
        for task_id, task in self.store.all().items():
            if not task.get("waiter") or _waiter_alive(task.get("waiter")):
                continue
            # A stop can cut the wait (wait_lost) or end the approval watch just before the process exits.
            waiting = task.get("status") in WAITING or task.get("status") == "wait_lost"
            approval = task.get("pending_approval") or task.get("settling_approval")
            last = task.get("last_approval") or {}
            if last.get("outcome") == "watch_lost":
                approval = approval or last
            if not waiting and not approval:
                continue
            found.append(task_id)
            profile, session_id = task.get("profile"), task.get("session_id")
            parts = [f"Task {task_id} ({profile}): the gateway restarted"]
            fields: dict = {"waiter": None, "pending_approval": None, "settling_approval": None}
            if waiting and not (session_id and task.get("admission_id")):
                fields.update(status="failed", error="the gateway restarted before the target accepted the task")
                parts.append(" before the target accepted it. Start it again.")
            elif waiting:
                fields.update(status="wait_lost", error="the gateway restarted while the task was waiting")
                parts.append(f" while this task was waiting, so its answer will not be injected here. Check it "
                             f"with profile_task action=status task_id={task_id}; the {profile} session "
                             f"{session_id} still exists (hermes://open/{session_id}).")
            else:
                parts.append(f" after the wait limit, while the {profile} session had an approval open.")
            if approval:
                fields["last_approval"] = {**approval, "outcome": "interrupted", "settled_at": time.time()}
                parts.append(f" The approval for `{approval.get('command')}` ended with the restart: the command did "
                             f"not run.")
            self.store.update(task_id, **fields)
            self._queue_only(task_id, "".join(parts), "restart")
        if any(task.get("outbox") for task in self.store.all().values()):
            threading.Thread(target=self._flush_when_hosted, name="profile-tasks-load", daemon=True).start()
        return found

    def _queue_only(self, task_id: str, message: str, kind: str) -> None:
        with self._outbox_lock:
            task = self.store.get(task_id) or {}
            outbox = list(task.get("outbox") or [])
            outbox.append({"id": uuid.uuid4().hex[:8], "kind": kind, "text": message, "queued_at": time.time(),
                           "attempts": []})
            self.store.update(task_id, outbox=outbox)

    def _flush_when_hosted(self) -> None:
        deadline = time.monotonic() + LOAD_FLUSH_WAIT_SECONDS
        while not self._gateway_host():
            if time.monotonic() > deadline:
                return
            time.sleep(1)
        self._flush_outbox("load")

    def correct(self, task_id: str, text: str) -> dict:
        task = self.store.get(task_id)
        if task is None or not task.get("session_id") or not task.get("admission_id"):
            raise ValueError(f"unknown or not started task {task_id}")
        if not text.strip():
            raise ValueError("text is required")
        context = contextvars.copy_context()
        box: dict = {}

        def run() -> None:
            try:
                box["result"] = asyncio.run(self._steer(task, text))
            except Exception as exc:  # noqa: BLE001
                box["error"] = f"{exc.__class__.__name__}: {exc}"

        thread = threading.Thread(target=context.run, args=(run,), name=f"profile-task-steer-{task_id}", daemon=True)
        thread.start()
        thread.join(ACK_TIMEOUT_SECONDS)
        if "result" in box:
            ack = box["result"]
            status = ack.get("status") if isinstance(ack, dict) else None
            if status in ("queued", "redirected"):
                return {"task_id": task_id, "corrected": True, "gateway": ack,
                        "note": "The correction reaches the running turn at its next step; the answer still "
                                "arrives as a new message."}
            return {"task_id": task_id, "corrected": False, "gateway": ack,
                    "note": "No turn is running to correct. Use action=follow_up to send it as the next message."}
        return {"task_id": task_id, "corrected": False, "error": box.get("error", "timed out"),
                "note": "The turn may already be finished. Use action=follow_up to send it as the next message."}

    async def _steer(self, task: dict, text: str) -> Any:
        async with _Connection(self._home(task["profile"])) as conn:
            receipt = await conn.rpc("prompt.receipt", session_id=task["session_id"], admission_id=task["admission_id"])
            generation = receipt.get("execution_generation")
            if receipt["status"] != "started" or generation is None:
                return {"status": "rejected", "reason": f"turn is {receipt['status']}"}
            return await conn.rpc("session.steer", session_id=task["session_id"], execution_generation=generation,
                                  text=text)

    def status(self, task_id: str | None) -> dict:
        tasks = self.store.all()
        if task_id:
            if task_id not in tasks:
                raise ValueError(f"unknown task {task_id}")
            tasks = {task_id: tasks[task_id]}
        extra: dict[str, dict] = {}
        for tid, task in tasks.items():
            if not _waiter_alive(task.get("waiter")) and (task.get("pending_approval")
                                                           or task.get("settling_approval")):
                self._drop_dead_approval(tid, "the process that watched the target session is gone")
                tasks[tid] = self.store.get(tid) or task
            if task.get("status") in REFRESHABLE and not (task.get("status") in WAITING and _waiter_alive(task.get("waiter"))):
                extra[tid] = self._refresh(task)
                tasks[tid] = self.store.get(tid) or task
        keep = ("task_id", "profile", "status", "session_id", "title", "delivered", "error", "created_at", "updated_at")
        rows = []
        for tid, t in tasks.items():
            row = {k: t.get(k) for k in keep}
            row["link"] = f"hermes://open/{t['session_id']}" if t.get("session_id") else None
            if t.get("pending_approval"):
                row["pending_approval"] = {k: t["pending_approval"].get(k) for k in
                                           ("command", "description", "expires_at")}
            if t.get("last_approval"):
                row["last_approval"] = {k: t["last_approval"].get(k) for k in ("command", "outcome", "user_summary")}
            if t.get("outbox"):
                row["notices_not_delivered"] = [e["text"] for e in t["outbox"]]
            if task_id and t.get("last_answer") and "answer" not in extra.get(tid, {}):
                row["answer"] = t["last_answer"]
            if "answer" in row or "answer" in extra.get(tid, {}):
                # This result hands the answer over; the queued injection of it would repeat it.
                self._drop_notices(tid, "result")
                row.pop("notices_not_delivered", None)
            rows.append(row | extra.get(tid, {}))
        return {"tasks": sorted(rows, key=lambda r: r.get("created_at") or 0), "targets": self.targets()}

    def _refresh(self, task: dict) -> dict:
        """Re-read the receipt of a task nobody waits for any more; record and return what it says."""
        task_id, profile, session_id = task["task_id"], task["profile"], task.get("session_id")
        restarted = task.get("status") in WAITING
        if restarted:
            restart_note = "The gateway restarted while this task was waiting, so no answer was injected. "
        elif task.get("status") == "wait_lost":
            restart_note = (f"The plugin stopped waiting for {profile} ({task.get('error')}), "
                            f"so no answer was injected. ")
        else:
            restart_note = ""
        if not session_id or not task.get("admission_id"):
            error = "the gateway restarted before the target accepted the task" if restarted else "not accepted yet"
            if restarted:
                self.store.update(task_id, status="failed", error=error)
            return {"note": f"{restart_note}The task was never accepted by {profile}. Start it again."}
        box: dict = {}
        context = contextvars.copy_context()

        def run() -> None:
            try:
                box["receipt"] = asyncio.run(self._read_receipt(task))
            except Exception as exc:  # noqa: BLE001 - reported to the manager as the status
                box["error"] = f"{exc.__class__.__name__}: {exc}"

        thread = threading.Thread(target=context.run, args=(run,), name=f"profile-task-status-{task_id}", daemon=True)
        thread.start()
        thread.join(ACK_TIMEOUT_SECONDS)
        receipt = box.get("receipt")
        if receipt is None:
            if restarted:
                self.store.update(task_id, status="wait_lost")
            return {"note": f"{restart_note}Could not read the turn's state from {profile}: "
                            f"{box.get('error', 'timed out')}. The {profile} session {session_id} still exists."}
        state = receipt.get("status")
        if state not in ("terminal", "unknown"):
            if restarted or task.get("status") == "wait_lost":
                self.store.update(task_id, status="waiting_timeout")
            return {"turn": state, "note": f"{restart_note}{profile} is still on it (turn {state}). The work is "
                                            f"not cancelled. Check back later with action=status."}
        ok, body = _final_text(receipt)
        if ok:
            self.store.update(task_id, status="answered", last_answer=body, delivered=True, delivered_by="status")
            return {"turn": state, "answer": body,
                    "note": f"{restart_note}Answer read from the {profile} session after the wait ended."}
        self.store.update(task_id, status="failed", error=body, delivered=True, delivered_by="status")
        if state == "unknown":
            return {"turn": state, "note": (
                f"{restart_note}Hermes marked the turn unknown: the gateway stopped while it ran, so it did "
                f"not finish ({body}). The {profile} session {session_id} is paused on that turn until a person "
                f"resends or discards it in the desktop; a follow_up queues behind it until then.")}
        return {"turn": state, "note": f"{restart_note}The turn did not complete: {body}. The {profile} "
                                       f"session {session_id} still exists."}

    async def _read_receipt(self, task: dict) -> dict:
        async with _Connection(self._home(task["profile"])) as conn:
            return await conn.rpc("prompt.receipt", session_id=task["session_id"], admission_id=task["admission_id"],
                                  include_result=True)


SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Hand a task to another Hermes profile (for example analyst, implementer, reviewer) served by this "
        "gateway. The task runs in its own session under that profile, with its own tools, MCP servers and "
        "tokens. action=start returns at once with a task id and the session; the final answer arrives later "
        "in this conversation as a new message, so end your turn after starting. action=follow_up sends a "
        "new message in the same task session once its turn has ended (refused while it runs). action=correct "
        "steers the turn that is running now. "
        "action=status lists tasks and their state; for a task nobody waits on any more (past the wait limit, "
        "or a gateway restart) it reads the target turn and returns the late answer or what happened. "
        "When a task session asks for an approval, a notice arrives here; action=approval shows that "
        "approve/deny prompt in this conversation and passes the answer to the task session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "follow_up", "correct", "status", "approval"]},
            "profile": {"type": "string", "description": "Target profile for action=start."},
            "text": {"type": "string", "description": "Task, follow-up or correction text."},
            "title": {"type": "string", "description": "Short title for the task session (action=start)."},
            "task_id": {"type": "string",
                        "description": "Task id for follow_up, correct, approval, or one task's status."},
        },
        "required": ["action"],
    },
}


def register(ctx: Any) -> None:
    missing = check_internals()
    if missing:
        logger.error("profile-tasks: Hermes internals missing after an update; the %s tool will refuse every "
                     "call until the plugin is fixed: %s", TOOL_NAME, "; ".join(missing))
    plugin = ProfileTasks(ctx, missing)
    if not missing:
        recovered = plugin.recover_after_restart()
        if recovered:
            logger.info("profile-tasks: tasks cut off by a gateway restart: %s", ", ".join(recovered))
    ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=SCHEMA, handler=plugin.handle,
                      description=SCHEMA["description"], emoji="📨")
    ctx.register_hook("post_approval_response", plugin.on_approval_response)
