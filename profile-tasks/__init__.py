"""profile-tasks: the manager hands a task to another profile served by the same Hermes gateway.

Each task is one session created under the target profile the way the desktop creates one
(``session.create`` with ``source="gui"``), then the text is submitted with ``prompt.submit``.
The target runs with its own config, MCP servers and tokens; only its final text comes back,
injected once into the calling conversation.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
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
RPC_METHODS = ("session.create", "session.resume", "prompt.submit", "prompt.receipt", "session.steer")
_REQUIRED_FIELDS = {
    "session.create": {"source", "request_id", "title"},
    "session.resume": {"session_id"},
    "prompt.submit": {"session_id", "text", "submission_id"},
    "prompt.receipt": {"session_id", "admission_id", "include_result"},
    "session.steer": {"session_id", "execution_generation", "text"},
}
_STATE_LOCK = threading.Lock()

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

    def __init__(self, home: Path) -> None:
        self.home = home
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
            if frame.get("type") == "message.complete" and (frame.get("payload") or {}).get("admission_id") == admission_id:
                return


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
        try:
            if action == "start":
                return tool_result(self.start(args.get("profile") or "", args.get("text") or "", args.get("title")))
            if action == "follow_up":
                return tool_result(self.follow_up(args.get("task_id") or "", args.get("text") or ""))
            if action == "correct":
                return tool_result(self.correct(args.get("task_id") or "", args.get("text") or ""))
            if action == "status":
                return tool_result(self.status(args.get("task_id")))
            return tool_error("action must be one of: start, follow_up, correct, status")
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
                          title=label, created_at=time.time(), status="starting", session_id=None, turns=[])
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
        caller_key = bound_session_key() or task["caller_session_key"]
        self.store.update(task_id, caller_session_key=caller_key)
        return self._send(task_id, text, create=False)

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
                self._deliver(task_id, f"Task {task_id} did not complete: {exc.__class__.__name__}: {exc}. "
                                       "The target session still exists.", status="failed")

    async def _send_and_wait(self, task_id: str, text: str, create: bool, accepted: dict,
                             ready: threading.Event) -> None:
        task = self.store.get(task_id)
        async with _Connection(self._home(task["profile"])) as conn:
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
            self.store.update(task_id, status="running", admission_id=admission_id, turns=turns)
            ready.set()
            deadline = time.monotonic() + self._wait_seconds()
            try:
                final = await conn.wait_terminal(session_id, admission_id, deadline)
            except TimeoutError:
                minutes = round(self._wait_seconds() / 60, 1)
                self._deliver(task_id, (
                    f"Task {task_id} ({task['profile']}): no answer within {minutes} minutes. The work is not "
                    f"cancelled. Check back later with profile_task action=status task_id={task_id}."),
                    status="waiting_timeout")
                return
        ok, body = _final_text(final)
        if ok:
            message = f"Answer from {task['profile']} for task {task_id} (session {session_id}):\n\n{body}"
            self._deliver(task_id, message, status="answered", last_answer=body)
        else:
            message = (f"Task {task_id} ({task['profile']}) did not complete: {body}\n"
                       f"The {task['profile']} session {session_id} still exists.")
            self._deliver(task_id, message, status="failed", error=body)

    def _deliver(self, task_id: str, message: str, *, status: str, **fields: Any) -> None:
        task = self.store.get(task_id) or {}
        injected = False
        try:
            injected = self.ctx.inject_message(message, session_key=task.get("caller_session_key"))
        except Exception:  # noqa: BLE001
            logger.warning("profile-tasks: injection for task %s raised", task_id, exc_info=True)
        if not injected:
            logger.warning("profile-tasks: the calling conversation refused the answer of task %s", task_id)
        self.store.update(task_id, status=status, delivered=bool(injected), **fields)

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
        keep = ("task_id", "profile", "status", "session_id", "title", "delivered", "error", "created_at", "updated_at")
        rows = [{k: t.get(k) for k in keep} | {"link": f"hermes://open/{t['session_id']}" if t.get("session_id") else None}
                for t in tasks.values()]
        return {"tasks": sorted(rows, key=lambda r: r.get("created_at") or 0), "targets": self.targets()}


SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Hand a task to another Hermes profile (for example analyst, implementer, reviewer) served by this "
        "gateway. The task runs in its own session under that profile, with its own tools, MCP servers and "
        "tokens. action=start returns at once with a task id and the session; the final answer arrives later "
        "in this conversation as a new message, so end your turn after starting. action=follow_up sends a "
        "new message in the same task session. action=correct steers the turn that is running now. "
        "action=status lists tasks and their state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "follow_up", "correct", "status"]},
            "profile": {"type": "string", "description": "Target profile for action=start."},
            "text": {"type": "string", "description": "Task, follow-up or correction text."},
            "title": {"type": "string", "description": "Short title for the task session (action=start)."},
            "task_id": {"type": "string", "description": "Task id for follow_up, correct, or one task's status."},
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
    ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=SCHEMA, handler=plugin.handle,
                      description=SCHEMA["description"], emoji="📨")
