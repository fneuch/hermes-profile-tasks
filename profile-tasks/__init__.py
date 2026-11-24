"""profile-tasks: the manager hands a task to another profile served by the same Hermes gateway.

Each task is one session created under the target profile the way the desktop creates one
(``session.create`` with ``source="gui"``), then the text is submitted with ``prompt.submit``.
The target runs with its own config, MCP servers and tokens; only its final text comes back,
injected once into the calling conversation.

A plugin backend with no calling conversation uses the module functions ``send`` and ``result``
instead: one task (one visible session) per caller-chosen key, its answer kept for ``result``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import fcntl
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOOL_NAME = "profile_task"
TOOLSET = "profile_tasks"
PLUGIN_ID = "profile-tasks"
# Sent on every task save; the desktop receives it as plugin.profile-tasks.tasks.changed.
CHANGED_EVENT = "tasks.changed"
STATE_KEY = "tasks"
DEFAULT_WAIT_SECONDS = 1800
ACK_TIMEOUT_SECONDS = 120
# After the wait limit the waiter keeps watching the target turn this long, only to relay approvals.
APPROVAL_WATCH_SECONDS = 24 * 3600
# A paused task with no new turn for this long is no longer followed (setting follow_timeout_seconds).
DEFAULT_FOLLOW_SECONDS = APPROVAL_WATCH_SECONDS
# While a notice is queued and refused, it is tried again this often.
OUTBOX_RETRY_SECONDS = 60
RELAY_RULE = "profile_task_relay"
# A process that loads the plugin but never hosts the gateway (CLI, plugin listing) stops waiting after this.
LOAD_FLUSH_WAIT_SECONDS = 300
APPROVAL_OUTCOMES = ("denied", "timeout", "cancelled", "notify_failed", "blocked")
# Target frames the waiter hands to the plugin: approvals, the tool result that shows how one ended, turn ends.
WATCHED_FRAMES = ("approval.request", "approval.settled", "tool.start", "tool.complete", "message.complete")
RPC_METHODS = ("session.create", "session.resume", "prompt.submit", "prompt.receipt", "session.steer",
               "approval.respond", "session.info")
_REQUIRED_FIELDS = {
    "session.create": {"source", "request_id", "title"},
    "session.resume": {"session_id"},
    "prompt.submit": {"session_id", "text", "submission_id"},
    "prompt.receipt": {"session_id", "admission_id", "include_result"},
    "session.steer": {"session_id", "execution_generation", "text"},
    "approval.respond": {"session_id", "choice"},
    "session.info": {"session_id"},
}
_STATE_LOCK = threading.Lock()
# An approval withdrawn this close before the wait was cut ended with the gateway stop.
STOP_GRACE_SECONDS = 5.0
WAITING = ("starting", "running")
REFRESHABLE = ("starting", "running", "waiting_timeout", "wait_lost", "waiting_subwork", "follow_lost")
# A task of the target profile that has not finished: its caller is still waiting for it.
OPEN_SUBTASK_STATES = ("starting", "running", "waiting_timeout", "waiting_subwork")
# Frames that show the target session is working on a turn.
TURN_ACTIVITY = ("message.start", "message.delta", "message.interim", "reasoning.delta", "thinking.delta",
                 "tool.start", "tool.complete", "tool.generating", "tool.progress", "approval.request")
# Internals only the pause rule's queued-turn and background-command conditions need; without them the rest works.
LIVE_SESSION_INTERNALS = ("tui_gateway.session_notifications", "tui_gateway.server.")
# The gateway stop flag; without it a turn cut by a stop is reported failed instead of left to the restart report.
STOP_INTERNALS = ("gateway.run_api",)
# Notices kept on a keyed task (newest last); its follow-up clears them.
KEYED_NOTICE_LIMIT = 20

TARGET_GUIDANCE = (
    "Nobody watches this session live and nobody can answer a clarify question here. "
    "If you need information, ask for it in your final answer. "
    "If you end a turn while something you started will bring you back to this session (a task given to "
    "another profile, a background command, a card, a CI run), end your message with a last line "
    "`WAITING: <what>`. Without it, your answer is taken as final."
)


def check_internals(ctx: Any = None) -> list[str]:
    """Names of the Hermes internals this plugin uses that the running Hermes no longer has.

    With *ctx*, also checks that the plugin context still exposes its plugin manager as ``_manager``
    and its ``on_unload`` registration.
    """
    missing: list[str] = []
    if ctx is not None and not hasattr(ctx, "_manager"):
        missing.append("hermes_cli.plugins_context.PluginContext._manager")
    if ctx is not None and not hasattr(ctx, "on_unload"):
        missing.append("hermes_cli.plugins_context.PluginContext.on_unload")

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
    state = need("hermes_cli.plugins_state", ("PluginState",))
    if state is not None and not hasattr(getattr(state, "PluginState", None), "data_dir"):
        missing.append("hermes_cli.plugins_state.PluginState.data_dir")
    need("hermes_constants", ("get_hermes_home",))
    need("gateway.session_context", ("bound_session_key", "get_session_env"))
    need("tools.approval", ("request_tool_approval", "list_gateway_approvals", "withdraw_gateway_approval"))
    need("tools.approval_context", ("get_current_session_key",))
    need("agent.i18n", ("supported_languages", "t"))
    plugins = need("hermes_cli.plugins", ("PluginManager",))
    if plugins is not None and not hasattr(getattr(plugins, "PluginManager", None), "has_gateway_message_injector"):
        missing.append("hermes_cli.plugins.PluginManager.has_gateway_message_injector")
    need("websockets.asyncio.client", ("connect",))
    need("tui_gateway.session_notifications", ("_session_owns_pending_process_notification",))
    need("gateway.run_api", ("_ACTIVE_RUNNER",))
    # The gateway server is heavy to import: it is read only where this process already runs it.
    served = sys.modules.get("tui_gateway.server")
    for name in ("_sessions", "_sessions_lock"):
        if served is not None and not hasattr(served, name):
            missing.append(f"tui_gateway.server.{name}")
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
            pass
    if isinstance(result, dict) and result.get("status") == "blocked":
        summary = str(result.get("user_summary") or "")
        return _summary_outcome(summary), summary
    refusal = _write_gate_refusal(result)
    if refusal is not None:
        return refusal
    return "ran", ""


# Phrases of the file-write approval gates (tools/file_tools_write_guards.py, English only) -> outcome key.
_WRITE_GATE_OUTCOMES = (("timed out", "timeout"), ("withdrawn", "cancelled"), ("denied", "denied"),
                        ("could not be delivered", "notify_failed"), ("Failed to send", "notify_failed"))


def _write_gate_refusal(result: Any) -> tuple[str, str] | None:
    """(outcome, refusal text) when a file-write gate refused the call, else None.

    Those gates return a plain ``{"error": "BLOCKED: write to ..."}``, without the ``status`` of a command gate.
    """
    text = result.get("error") if isinstance(result, dict) else result
    if not isinstance(text, str) or not text.startswith("BLOCKED:"):
        return None
    for phrase, outcome in _WRITE_GATE_OUTCOMES:
        if phrase in text:
            return outcome, text
    return "not_run", text


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


def _caller_session_id() -> str | None:
    """The calling conversation's durable session id (the desktop's stored session id), or None."""
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_ID") or None


def _announce(task_id: str) -> None:
    """Tell the desktop's status-bar item (desktop/plugin.js) that a task changed, so it reads the list again."""
    # Only a process already serving desktop clients delivers it; importing the gateway server elsewhere is costly.
    if "tui_gateway.server" not in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, CHANGED_EVENT, {"task_id": task_id})
    except Exception as exc:  # noqa: BLE001 - the item also reads the list on a slow poll
        logger.debug("profile-tasks: change event for %s not sent: %s", task_id, exc)


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
        return self.modify(task_id, lambda _task: fields)

    def modify(self, task_id: str, change: Any) -> dict | None:
        """Store ``change(task)``'s fields in the task, read and written under one lock; None changes nothing.

        ``change`` must not call the store: it runs under _STATE_LOCK, which is not reentrant.
        """
        with self._locked():
            tasks = dict(self._state.get(STATE_KEY, {}) or {})
            task = dict(tasks.get(task_id, {}))
            fields = change(dict(task))
            if fields is None:
                return None
            task.update(fields, updated_at=time.time())
            tasks[task_id] = task
            self._state.set(STATE_KEY, tasks)
        _announce(task_id)
        return task

    @contextlib.contextmanager
    def _locked(self):
        # Every process that loads the plugin (the gateway, a launcher that then attaches to it) writes this state.
        with _STATE_LOCK:
            path = Path(self._state.data_dir) / "tasks.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield


# ---- gateway client ---------------------------------------------------------------------------


class _Connection:
    """One websocket connection to a profile's gateway, opened with a ticket bound to that profile."""

    def __init__(self, home: Path, on_frame: Any = None) -> None:
        self.home = home
        self.on_frame = on_frame
        self._stack = None
        self.client = None
        # True while an observer reads the events: wait_terminal then waits for the turn it hands over.
        self.pumping = False
        self._completions: dict[str, asyncio.Event] = {}

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
                if self.pumping:
                    await self._settled(admission_id)
                else:
                    self._drain()
                return receipt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                async with asyncio.timeout(min(backoff, remaining)):
                    await self._completion(admission_id)
            except TimeoutError:
                backoff = min(backoff * 2, 5.0)

    async def _settled(self, admission_id: str) -> None:
        """With an observer reading the events: give it a moment to hand over the turn's end frame."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(1.0):
                await self._completions.setdefault(admission_id, asyncio.Event()).wait()

    def completed(self, frame: dict) -> None:
        """Called by the observer once the end of a turn is handled: wakes the wait for that admission."""
        admission_id = (frame.get("payload") or {}).get("admission_id")
        if admission_id:
            self._completions.setdefault(admission_id, asyncio.Event()).set()

    def _drain(self) -> None:
        """Hand over the frames already queued: the gateway sent them before the terminal receipt."""
        while True:
            try:
                event = self.client.events.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(event, Exception):
                # The connection ended after the receipt was read; nothing more can be queued.
                return
            self._dispatch(event)

    def _dispatch(self, event: dict) -> dict:
        frame = event.get("params") or {}
        if self.on_frame is not None and frame.get("type") in WATCHED_FRAMES:
            try:
                self.on_frame(frame)
            except Exception:  # noqa: BLE001 - a relay failure must not end the wait
                logger.warning("profile-tasks: target frame handling failed", exc_info=True)
        return frame

    async def _completion(self, admission_id: str) -> None:
        if self.pumping:
            await self._completions.setdefault(admission_id, asyncio.Event()).wait()
            return
        while True:
            event = await self.client.events.get()
            if isinstance(event, Exception):
                raise event
            frame = self._dispatch(event)
            if frame.get("type") == "message.complete" and (frame.get("payload") or {}).get("admission_id") == admission_id:
                return


class _Observer:
    """The one connection that follows a task's target session: bound to it, reading every event it sends.

    It lives in the asyncio loop of the thread that entered it. Other threads put their calls on that loop with
    ``submit``/``call``, so nothing else resumes (and takes over) the session.
    """

    def __init__(self, plugin: "ProfileTasks", task_id: str, home: Path) -> None:
        self.plugin, self.task_id, self.home = plugin, task_id, home
        self.conn: _Connection | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.busy = False
        self.turn_admission: str | None = None
        self.ended: set[str] = set()
        # Monotonic time after which a paused task with no new turn is given up; None while nothing is paused.
        self.deadline: float | None = None
        self.error: BaseException | None = None
        self.closed = False
        self._stop: asyncio.Event | None = None
        self._pump_task: asyncio.Task | None = None
        self._cm: _Connection | None = None

    async def __aenter__(self) -> "_Observer":
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        task_id = self.task_id
        self._cm = _Connection(self.home, on_frame=lambda frame: self.plugin._on_target_frame(task_id, frame))
        self.conn = await self._cm.__aenter__()
        if hasattr(getattr(self.conn, "client", None), "events"):
            self.conn.pumping = True
            self._pump_task = asyncio.create_task(self._pump())
            self._pump_task.add_done_callback(self._pump_done)
        with self.plugin._observers_lock:
            self.plugin._observers[task_id] = self
        return self

    async def __aexit__(self, *exc) -> None:
        self.closed = True
        with self.plugin._observers_lock:
            if self.plugin._observers.get(self.task_id) is self:
                del self.plugin._observers[self.task_id]
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(BaseException):
                await self._pump_task
        await self._cm.__aexit__(*exc)

    def _pump_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.error = task.exception()
            self._stop.set()

    async def _pump(self) -> None:
        events = self.conn.client.events
        while True:
            event = await events.get()
            if isinstance(event, Exception):
                raise event
            frame = self.conn._dispatch(event)
            kind = frame.get("type")
            payload = frame.get("payload") or {}
            if kind == "message.complete":
                try:
                    await self.plugin._turn_ended(self.task_id, frame, self)
                except Exception:  # noqa: BLE001 - one turn's handling must not end the observation
                    logger.warning("profile-tasks: end of a turn of task %s not handled", self.task_id, exc_info=True)
                # Busy until the end is handled, so no follow_up starts a turn while it is.
                self.busy = False
                self.conn.completed(frame)
            elif kind in TURN_ACTIVITY:
                self.busy = True
                if kind.startswith("message.") and payload.get("admission_id"):
                    self.turn_admission = payload["admission_id"]

    def submit(self, make: Any) -> Any:
        """Run ``make(connection)`` on the observer's loop; a concurrent future."""
        return asyncio.run_coroutine_threadsafe(make(self.conn), self.loop)

    def call(self, make: Any, timeout: float = ACK_TIMEOUT_SECONDS) -> Any:
        return self.submit(make).result(timeout)

    def stop(self) -> None:
        """Ask the observer to close its connection (callable from any thread)."""
        if self._stop is not None and self.loop is not None and not self.closed:
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._stop.set)

    async def idle(self) -> None:
        """Keep reading until stopped; give up a paused task at its follow limit. Raises what ended the pump."""
        while self._pump_task is not None and not self._stop.is_set():
            wait = 1.0 if self.deadline is None or self.busy else max(0.0, min(1.0, self.deadline - time.monotonic()))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await self._stop.wait()
            if self.deadline is not None and not self.busy and time.monotonic() >= self.deadline:
                self.deadline = None
                self.plugin._follow_limit(self.task_id, self)
        if self.error is not None:
            raise self.error
        # A connection with no event stream (nothing to read) has nothing to follow.


# Process states of a process that no longer runs: Linux /proc letters, then psutil names (macOS).
_ENDED_STATES = ("Z", "X", "zombie", "dead")


def _process_state(pid: int) -> tuple[str, str] | None:
    """(start time, state) of *pid*, or None when no such process can be read.

    Read from /proc where there is one (Linux), else with psutil, a Hermes dependency (macOS).
    """
    if Path("/proc/self/stat").exists():
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19], fields[0]
        except (OSError, IndexError):
            return None
    import psutil
    try:
        proc = psutil.Process(pid)
        return f"{proc.create_time():.6f}", proc.status()
    except psutil.Error:
        return None


def _process_token(pid: int) -> str:
    """pid plus its start time, so a reused pid does not pass for the waiting process."""
    state = _process_state(pid)
    return f"{pid}:{state[0] if state else ''}"


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
    state = _process_state(pid)
    # A killed waiter stays a zombie until its parent reaps it; it no longer waits.
    if state is None or state[1] in _ENDED_STATES:
        return False
    return f"{pid}:{state[0]}" == token


def _ended_by_stop(task: dict, last: dict) -> bool:
    """True if the approval was withdrawn as the stopping gateway ended the turn, just before it cut the wait."""
    if last.get("outcome") not in ("cancelled", "interrupted") or "raised_at" not in last:
        return False
    ended = last.get("settled_at") or last["raised_at"] + float(last.get("settled_after_s") or 0)
    # Only the latest cut can be this stop; an older one was an earlier, plain disconnect.
    cuts = [e["queued_at"] for e in list(task.get("outbox") or []) + list(task.get("notices") or [])
            if e.get("kind") == "wait_lost"]
    return bool(cuts) and 0 <= max(cuts) - ended <= STOP_GRACE_SECONDS


def _cut_approvals(task: dict) -> list[dict]:
    """The approvals a restart ended: the open ones, else the last one a stop withdrew or left unwatched."""
    approvals = list(_approvals(task).values())
    last = task.get("last_approval") or {}
    if not approvals and (last.get("outcome") == "watch_lost" or _ended_by_stop(task, last)):
        approvals = [last]
    return approvals


def _approvals(task: dict) -> dict[str, dict]:
    """The task's open approvals by prompt_id, oldest first; ``stage`` is ``pending`` or ``settling``.

    A task stored by an earlier version has only the single ``pending_approval``/``settling_approval`` fields.
    """
    found: dict[str, dict] = {}
    if "approvals" in task:
        found = {p: dict(a) for p, a in (task.get("approvals") or {}).items()}
    else:
        for field, stage in (("settling_approval", "settling"), ("pending_approval", "pending")):
            legacy = task.get(field)
            if legacy and legacy.get("prompt_id"):
                found[legacy["prompt_id"]] = {**legacy, "stage": stage}
    return dict(sorted(found.items(), key=lambda item: item[1].get("raised_at") or 0))


def _approval_fields(approvals: dict[str, dict]) -> dict:
    """Store fields for *approvals*; ``pending_approval``/``settling_approval`` show the oldest of each stage."""
    def oldest(stage: str) -> dict | None:
        return next((a for a in approvals.values() if a.get("stage") == stage), None)
    return {"approvals": approvals, "pending_approval": oldest("pending"), "settling_approval": oldest("settling")}


def _session_ref(profile: str | None, session_id: str | None) -> str:
    """The desktop's session reference: a clickable chip that opens the session on its owning profile.

    The desktop matches it up to the next whitespace, so it must stand alone between spaces or line ends.
    """
    return f"@session:{profile}/{session_id}"


def _wait_limit_text(task_id: str, task: dict, minutes: float) -> str:
    check = ("result() reads the answer once the turn ends" if task.get("key")
             else f"Check back later with profile_task action=status task_id={task_id}")
    return f"{_label(task_id, task)}: no answer within {minutes} minutes. The work is not cancelled. {check}."


def _label(task_id: str, task: dict) -> str:
    """How a notice names its task: by key for a keyed caller, which never sees task ids."""
    if task.get("key"):
        return f"Key {task['key']} ({task.get('profile')})"
    return f"Task {task_id} ({task.get('profile')})"


def _notice(message: str, kind: str) -> dict:
    """One outbox entry for the calling conversation."""
    return {"id": uuid.uuid4().hex[:8], "kind": kind, "text": message, "queued_at": time.time(), "attempts": []}


def _keyed_notices(current: list, new: list, trigger: str) -> list:
    """A keyed task's recorded notices plus *new*, capped; an answer's text is not kept (``last_answer`` has it)."""
    added = [{k: v for k, v in e.items() if not (k == "text" and e["kind"] == "result")}
             | {"delivered_at": None, "trigger": trigger} for e in new]
    return (list(current) + added)[-KEYED_NOTICE_LIMIT:]


def _reauth_server(payload: dict) -> str | None:
    """The MCP server a tool.complete result names as needing re-authentication, or None."""
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if isinstance(result, dict) and result.get("needs_reauth") is True and result.get("server"):
        return str(result["server"])
    return None


_AUTH_TEXT = re.compile(r"\b401\b|unauthori[sz]ed|re-?authenticat|invalid[_ ]token|authentication (failed|required)",
                        re.IGNORECASE)


def _mcp_tool_error_text(payload: dict) -> str:
    """The error text of a failed tool.complete result, or "" when the call did not fail."""
    result = payload.get("result")
    if isinstance(result, str) and result.startswith("{"):
        with contextlib.suppress(ValueError):
            result = json.loads(result)
    if isinstance(result, dict):
        # A failed result without an "error" key: its whole body is the error text.
        return str(result.get("error") or (json.dumps(result) if payload.get("is_error") else ""))
    return str(result or "") if payload.get("is_error") else ""


def _mcp_runtime(profile_home: Path) -> dict[str, Any]:
    """The target profile's MCP connections in this process, by server name (keys are ``(profile home, name)``)."""
    from tools import mcp_tool as core
    home = profile_home.resolve()
    with core._lock:
        items = list(core._servers.items())
    runtime = {}
    for key, server in items:
        if not (isinstance(key, tuple) and len(key) == 2):
            # e.g. a session tool scope's connection: not a profile's own server.
            logger.debug("profile-tasks: MCP connection key %r skipped (not (profile home, name))", key)
            continue
        if Path(str(key[0])).resolve() == home:
            runtime[key[1]] = (key, server)
    return runtime


def _mcp_rejected_auth(server: Any) -> bool:
    """Whether the last HTTP error this connection received is a 401 (``_http_rejection``, recorded by Hermes
    because the MCP SDK folds it into a generic MCPError)."""
    return (getattr(server, "_http_rejection", None) or {}).get("status") == 401


def _mcp_set_aside(key: Any, server: Any) -> str | None:
    """Why Hermes stopped using a connection whose last HTTP error is a 401, or None: an open circuit breaker
    (status stays "connected"), or a park (no live session)."""
    from tools import mcp_tool as core
    if not _mcp_rejected_auth(server):
        return None
    failures = core._server_error_counts.get(key, 0)
    opened = core._server_breaker_opened_at.get(key)
    if (failures >= core._CIRCUIT_BREAKER_THRESHOLD and opened is not None
            and time.monotonic() - opened < core._CIRCUIT_BREAKER_COOLDOWN_SEC):
        return (f"Hermes paused it after {failures} consecutive failed calls, the last one an HTTP 401 "
                f"(unauthorized)")
    if getattr(server, "session", None) is None and getattr(server, "_park_reason", None):
        return f"Hermes set it aside ({server._park_reason}) after an HTTP 401 (unauthorized)"
    return None


def _mcp_auth_problem(row: dict) -> str | None:
    """Why a ``session.info`` ``mcp_servers`` row may need a new sign-in, or None."""
    if row.get("needs_auth"):
        return "its sign-in was rejected"
    if row.get("transport") != "stdio" and row.get("status") == "failed":
        return "it failed to connect, an expired sign-in being a common cause"
    return None


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
        self._key_lock = threading.Lock()
        # Held while a target frame is handled and while a follow_up reserves the next turn: an earlier turn's
        # frame is handled whole before the turn changes, or seen as stale after.
        self._turn_lock = threading.RLock()
        # task_id -> tool call ids of the target turn that started and have not completed yet.
        self._open_calls: dict[str, set[str]] = {}
        # task_id -> the observer that follows the task's session in this process.
        self._observers: dict[str, _Observer] = {}
        self._observers_lock = threading.Lock()
        # Set by register() when the queued-turn and background-command internals are missing.
        self.soft_missing: list[str] = []
        self.retry_seconds = OUTBOX_RETRY_SECONDS
        self._retry_thread: threading.Thread | None = None

    def _gateway_host(self) -> bool:
        """True once this process runs a gateway that accepts plugin messages."""
        return bool(getattr(getattr(self.ctx, "_manager", None), "has_gateway_message_injector", False))

    @staticmethod
    def _gateway_stopping() -> bool:
        """True while the gateway running this plugin drains or stops; False when that cannot be read."""
        try:
            from gateway import run_api
            runner = getattr(run_api, "_ACTIVE_RUNNER", None)
            return runner is not None and bool(getattr(runner, "_draining", False) or not getattr(runner, "_running", True))
        except Exception:  # noqa: BLE001 - an unreadable flag means the gateway is taken as live
            return False

    # settings
    def _wait_seconds(self) -> float:
        value = self.ctx.get_config("wait_timeout_seconds", DEFAULT_WAIT_SECONDS)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULT_WAIT_SECONDS)

    def _follow_seconds(self) -> float:
        value = self.ctx.get_config("follow_timeout_seconds", DEFAULT_FOLLOW_SECONDS)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULT_FOLLOW_SECONDS)

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
        self._check_new(profile, text)
        from gateway.session_context import bound_session_key
        caller_key = bound_session_key()
        if not caller_key:
            raise ValueError("no calling conversation to deliver the answer to")
        return self._send(self._new_task(profile, title, caller_key), text, create=True)

    def _check_new(self, profile: str, text: str) -> None:
        targets = self.targets()
        if profile not in targets:
            raise ValueError(f"profile must be one of {targets}")
        if not text.strip():
            raise ValueError("text is required")

    def _new_task(self, profile: str, title: str | None, caller_key: str | None, key: str | None = None) -> str:
        task_id = "t-" + uuid.uuid4().hex[:8]
        label = f"Task {task_id}" + (f": {title.strip()[:80]}" if title and title.strip() else "")
        self.store.update(task_id, task_id=task_id, profile=profile, caller_session_key=caller_key,
                          caller_session_id=_caller_session_id() if caller_key else None, title=label,
                          created_at=time.time(), status="starting", session_id=None, turns=[],
                          waiter=_process_token(os.getpid()), turn_token=uuid.uuid4().hex,
                          **({"key": key} if key else {}))
        return task_id

    # programmatic surface: a keyed task has no calling conversation; its caller reads it with result()
    def task_for_key(self, key: str) -> dict | None:
        """The latest task started under *key*, or None."""
        tasks = [t for t in self.store.all().values() if t.get("key") == key]
        return max(tasks, key=lambda t: t.get("created_at") or 0) if tasks else None

    def send_keyed(self, key: str, profile: str, text: str, title: str | None = None) -> dict:
        """Start *key*'s task in *profile*, or send *text* as a follow-up in that task's session."""
        key = (key or "").strip()
        if not key:
            raise ValueError("key is required")
        # Read before the lock: the receipt is a network call of up to ACK_TIMEOUT_SECONDS.
        earlier = self.task_for_key(key)
        read = self._read_turn(earlier) if earlier is not None and not earlier.get("session_id") else None
        with self._key_lock:
            task = self.task_for_key(key)
            starting = self._running_turn(task, read) if task is not None and not task.get("session_id") else None
            # A task that never got a session cannot take a follow-up: the key starts a new one.
            if task is None or (not task.get("session_id") and not starting):
                self._check_new(profile, text)
                task_id, create = self._new_task(profile, title, None, key), True
            elif profile != task["profile"]:
                raise ValueError(f"key {key} belongs to profile {task['profile']}, not {profile}")
            elif starting:
                raise ValueError(f"key {key} has a turn still running in {task['profile']} ({starting}); send "
                                 f"again once result() returns its answer")
            else:
                task_id, create = task["task_id"], False
        sent_at = time.time()
        sent = self._send(task_id, text, create=True) if create else self.follow_up(task_id, text, keyed=True)
        if not create and sent.get("accepted"):
            # The notices of the earlier turns are not this turn's.
            with self._outbox_lock:
                self.store.modify(task_id, lambda current: {"notices": [
                    e for e in current.get("notices") or [] if (e.get("queued_at") or 0) >= sent_at]})
        sent.pop("note", None)
        return {"key": key, **sent}

    def keyed_result(self, key: str) -> dict:
        """The state of *key*'s task and, once its last turn answered, that final answer."""
        task = self.task_for_key((key or "").strip())
        if task is None:
            raise ValueError(f"unknown key {key}")
        row = self.status(task["task_id"])["tasks"][0]
        keep = ("task_id", "profile", "status", "session_id", "link", "answer", "update", "error", "pending_approval")
        out = {"key": task["key"], **{k: row[k] for k in keep if row.get(k) is not None}}
        current = self.store.get(task["task_id"]) or {}
        # Wait limit, approvals, MCP sign-in and restart notices; the answer itself is in ``answer``.
        notices = [{k: e.get(k) for k in ("kind", "text", "queued_at")} for e in current.get("notices") or []
                   if e.get("text")]
        if notices:
            out["notices"] = notices
        if "answer" in out and not current.get("delivered"):
            self.store.update(task["task_id"], delivered=True, delivered_by="result")
        return out

    def follow_up(self, task_id: str, text: str, *, keyed: bool = False) -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        if not task.get("session_id"):
            raise ValueError(f"task {task_id} has no session yet")
        if not text.strip():
            raise ValueError("text is required")
        from gateway.session_context import bound_session_key
        # A followed session says itself whether a turn runs; else read the receipt before the lock: a network call.
        observer = self._live_observer(task_id)
        read = None if observer is not None else self._read_turn(task)
        with self._send_lock:
            # Re-read under the lock: a concurrent follow_up may have reserved the next turn since the read above.
            task = self.store.get(task_id) or task
            running = self._running_turn(task, read) if observer is None else self._observer_turn(task, observer)
            if running:
                raise ValueError(
                    f"task {task_id} has a turn running in {task['profile']} ({running}). A follow_up now would "
                    f"start a second turn on the same task. Use action=correct to change the running turn, or "
                    f"send the follow_up after its answer arrives.")
            # The previous turn's answer, error and admission must not pass for this turn's in status or result().
            fields = {"status": "starting", "last_answer": None, "last_update": None, "error": None,
                      "admission_id": None,
                      "waiter": _process_token(os.getpid()), "turn_token": uuid.uuid4().hex}
            if not keyed:
                bound = bound_session_key()
                fields["caller_session_key"] = bound or task["caller_session_key"]
                # Without a bound key the session id could come from another conversation's os.environ.
                fields["caller_session_id"] = (bound and _caller_session_id()) or task.get("caller_session_id")
            with self._turn_lock:
                self.store.update(task_id, **fields)
        return self._send(task_id, text, create=False)

    def _read_turn(self, task: dict) -> dict | None:
        """The receipt of the task's latest turn, tagged with its admission id; None when it has none."""
        if not task.get("admission_id"):
            return None
        return {**self._run_async(self._read_receipt_ok(task)), "admission_id": task["admission_id"]}

    @staticmethod
    def _observer_turn(task: dict, observer: _Observer) -> str | None:
        """Why a followed task's turn counts as running (a short reason), or None when a follow_up may start."""
        if task.get("status") == "starting" and _waiter_alive(task.get("waiter")):
            return "being submitted"
        if task.get("status") in WAITING:
            return "a turn is running"
        return "a turn is running" if observer.busy else None

    @staticmethod
    def _running_turn(task: dict, read: dict | None) -> str | None:
        """Why the task's turn counts as running (a short reason), or None when a follow_up may start.

        *read* is ``_read_turn`` of an earlier snapshot, taken before the caller's lock: no network call is made here.
        """
        if task.get("status") == "starting" and _waiter_alive(task.get("waiter")):
            return "being submitted"
        if not task.get("admission_id"):
            return None
        if read is None or read.get("admission_id") != task["admission_id"]:
            return "a newer turn started while its state was read"
        answer = read
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
            return {"task_id": task_id, "accepted": False, "error": accepted["error"]}
        task = self.store.get(task_id)
        return {
            "task_id": task_id, "accepted": True, "profile": task["profile"], "session_id": task["session_id"],
            "session_title": task["title"], "link": _session_ref(task["profile"], task["session_id"]),
            "note": (f"The {task['profile']} profile is working on it. Finish your turn now without waiting: "
                     "its final answer will arrive in this conversation as a new message."),
        }

    def _run_send(self, task_id: str, text: str, create: bool, accepted: dict, ready: threading.Event) -> None:
        # The turn this worker owns; a later follow_up replaces the token and the task state with it.
        turn = (self.store.get(task_id) or {}).get("turn_token")
        try:
            asyncio.run(self._send_and_wait(task_id, text, create, accepted, ready))
        except Exception as exc:  # noqa: BLE001 - every failure is reported to the manager
            logger.warning("profile-tasks: task %s failed", task_id, exc_info=True)
            error = f"{exc.__class__.__name__}: {exc}"
            self._end_calls(task_id, turn)
            if not ready.is_set():
                accepted["error"] = error
                # Stored here, not by _send: after the ack timeout nobody else records the failure.
                self._record(task_id, turn, status="failed", error=error, waiter=None)
                ready.set()
            else:
                self._wait_cut(task_id, turn, error)

    def _wait_cut(self, task_id: str, turn: str | None, error: str) -> None:
        """The plugin lost its connection to the target session: record it and tell the manager."""
        if self._gateway_stopping():
            # The gateway is stopping: the task stays as it is for recover_after_restart.
            return
        task = self.store.get(task_id) or {}
        if task.get("status") not in (*WAITING, "waiting_timeout", "waiting_subwork"):
            # The task already ended (the observer closing cuts the connection of its own last wait).
            return
        if task.get("status") == "waiting_subwork":
            # The observer of a paused task is not tied to one turn.
            turn = None
        # A newer turn's approval is not this worker's to drop.
        lost = (self._drop_dead_approval(task_id, "the plugin lost its connection to the target session")
                if turn is None or task.get("turn_token") == turn else "")
        check = ("result() reads it" if task.get("key")
                 else f"Check with profile_task action=status task_id={task_id}")
        head = _label(task_id, task) if task.get("key") else f"Task {task_id}"
        self._deliver(task_id, f"{head}: the wait for the answer was cut off ({error}). The work in "
                               f"the target session may still finish. {check}.{lost}", status="wait_lost",
                      error=error, kind="wait_lost", turn=turn)

    async def _send_and_wait(self, task_id: str, text: str, create: bool, accepted: dict,
                             ready: threading.Event) -> None:
        task = self.store.get(task_id)
        # Read before the target accepts the text: no follow_up can replace it until then.
        turn = task.get("turn_token")
        live = None if create else self._live_observer(task_id)
        if live is not None:
            # The task's observer already holds the session: the text goes through its connection.
            await asyncio.wrap_future(live.submit(lambda _conn: self._submit_and_wait(
                live, task_id, text, False, accepted, ready, turn, resume=False)))
            return
        async with _Observer(self, task_id, self._home(task["profile"])) as observer:
            await self._submit_and_wait(observer, task_id, text, create, accepted, ready, turn, resume=not create)
            # The turns that follow arrive by themselves; the observer stays until the task's real end.
            await observer.idle()

    async def _submit_and_wait(self, observer: _Observer, task_id: str, text: str, create: bool, accepted: dict,
                               ready: threading.Event, turn: str | None, *, resume: bool) -> None:
        """Submit *text* on the observer's connection and wait for that turn; its end is handled by the observer."""
        conn = observer.conn
        task = self.store.get(task_id)
        if create:
            snapshot = await conn.rpc("session.create", source="gui", request_id=task_id, title=task["title"])
            session_id = snapshot["session_id"]
            self.store.update(task_id, session_id=session_id, stored_session_id=snapshot.get("stored_session_id"))
            payload = f"{text}\n\n[{TARGET_GUIDANCE}]"
        else:
            session_id = task["session_id"]
            if resume:
                resumed = await conn.rpc("session.resume", session_id=session_id, omit_messages=True)
                if isinstance(resumed, dict) and resumed.get("stored_session_id"):
                    self.store.update(task_id, stored_session_id=resumed["stored_session_id"])
            payload = text
        submission_id = uuid.uuid4().hex
        with self._turn_lock:
            # Tool calls of an earlier turn that never reported back cannot gate this turn's approvals.
            self._open_calls.pop(task_id, None)
        observer.busy, observer.deadline, observer.turn_admission = True, None, None
        receipt = await conn.rpc("prompt.submit", session_id=session_id, text=payload, submission_id=submission_id)
        admission_id = receipt["admission_id"]
        observer.turn_admission = admission_id
        turns = list(self.store.get(task_id).get("turns") or []) + [admission_id]
        self.store.update(task_id, status="running", admission_id=admission_id, turns=turns,
                          waiter=_process_token(os.getpid()))
        ready.set()
        await self._check_mcp_auth(conn, task_id)
        deadline = time.monotonic() + self._wait_seconds()
        try:
            final = await conn.wait_terminal(session_id, admission_id, deadline)
        except TimeoutError:
            minutes = round(self._wait_seconds() / 60, 1)
            # Progress only: the caller is not told; status, the session and the status bar show it.
            self._deliver(task_id, _wait_limit_text(task_id, task, minutes), status="waiting_timeout",
                          kind="wait_limit", turn=turn, tell=False)
            # The observer keeps following the session; its turn end still reaches the manager.
            observer.deadline = time.monotonic() + self._follow_seconds()
            self._end_calls(task_id, turn)
            return
        if admission_id not in observer.ended:
            # No end frame came with the receipt: the receipt is the only record of this turn.
            ok, body = _final_text(final)
            if not ok and self._gateway_stopping():
                # The stop cut the turn: left for recover_after_restart.
                self._end_calls(task_id, turn)
                return
            await self._turn_ended(task_id, {"session_id": session_id, "type": "message.complete", "payload": {
                "admission_id": admission_id, "text": body if ok else "", "status": "complete" if ok else "error",
                "error": None if ok else body}}, observer)
        self._end_calls(task_id, turn)

    def _live_observer(self, task_id: str) -> _Observer | None:
        with self._observers_lock:
            observer = self._observers.get(task_id)
        if observer is None or observer.closed or observer.loop is None or observer.loop.is_closed():
            return None
        return None if observer._stop is not None and observer._stop.is_set() else observer

    def _ensure_observer(self, task_id: str) -> _Observer:
        """The task's live observer, starting one (it resumes the session) when none lives, e.g. after a restart."""
        observer = self._live_observer(task_id)
        if observer is not None:
            return observer
        ready = threading.Event()
        box: dict = {}
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(self._run_observer, task_id, ready, box),
                                  name=f"profile-task-observer-{task_id}", daemon=True)
        thread.start()
        if not ready.wait(ACK_TIMEOUT_SECONDS):
            raise RuntimeError(f"the target gateway did not take the session of task {task_id} in {ACK_TIMEOUT_SECONDS}s")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box["observer"]

    def _run_observer(self, task_id: str, ready: threading.Event, box: dict) -> None:
        try:
            asyncio.run(self._observe(task_id, ready, box))
        except Exception as exc:  # noqa: BLE001
            logger.warning("profile-tasks: following task %s failed", task_id, exc_info=True)
            error = f"{exc.__class__.__name__}: {exc}"
            if not ready.is_set():
                box["error"] = error
                ready.set()
            else:
                self._wait_cut(task_id, None, error)

    async def _observe(self, task_id: str, ready: threading.Event, box: dict) -> None:
        task = self.store.get(task_id) or {}
        async with _Observer(self, task_id, self._home(task["profile"])) as observer:
            resumed = await observer.conn.rpc("session.resume", session_id=task["session_id"], omit_messages=True)
            if isinstance(resumed, dict) and resumed.get("stored_session_id"):
                self.store.update(task_id, stored_session_id=resumed["stored_session_id"])
            box["observer"] = observer
            ready.set()
            await observer.idle()

    def _end_calls(self, task_id: str, turn: str | None) -> None:
        """Forget the task's open tool calls once *turn* stopped watching, unless a newer turn owns the task."""
        with self._turn_lock:
            if turn is None or (self.store.get(task_id) or {}).get("turn_token") == turn:
                self._open_calls.pop(task_id, None)

    # the end of every turn of a followed session
    async def _turn_ended(self, task_id: str, frame: dict, observer: _Observer) -> None:
        """A turn of the task's session ended, whoever started it: tell the manager, finished or not."""
        payload = frame.get("payload") or {}
        task = self.store.get(task_id) or {}
        if frame.get("session_id") not in (None, task.get("session_id")):
            return
        if payload.get("status") in ("interrupted", "error") and self._gateway_stopping():
            # The stop cut this turn: left as is, recover_after_restart reports it at the next load.
            return
        admission = payload.get("admission_id")
        if admission and admission in observer.ended:
            return
        if admission:
            observer.ended.add(admission)
        # Servers that failed during the turn, before its end is told.
        if observer.conn is not None:
            await self._check_mcp_auth(observer.conn, task_id)
        task = self.store.get(task_id) or {}
        submitted = (admission == task.get("admission_id") if admission
                     else task.get("status") in (*WAITING, "waiting_timeout"))
        if submitted and task.get("status") in ("answered", "failed") and task.get("delivered_by") == "status":
            # status already handed this turn's end to the manager past the wait limit.
            observer.deadline = None
            observer.stop()
            return
        if not submitted:
            known = list(task.get("turns") or [])
            turn_id = admission or f"observed-{sum(1 for t in known if str(t).startswith('observed-')) + 1}"
            self.store.update(task_id, turns=known + [turn_id], **({"admission_id": admission} if admission else {}))
        status = payload.get("status") or "complete"
        text = payload.get("text")
        text = text if isinstance(text, str) else ("" if text is None else json.dumps(text))
        self.store.update(task_id, told_text=text, told_at=time.time())
        failure = ""
        if status != "complete":
            cause = payload.get("error") or payload.get("failure_reason") or status
            failure = f"{cause}" + (f"\n\nPartial answer:\n{text}" if text and text != cause else "")
        reasons = self._pause_reasons(task, text)
        profile, session_id = task.get("profile"), task.get("session_id")
        if reasons:
            what = ", ".join(reasons)
            lead = (f"Update from {profile} for task {task_id} (session {session_id}). Not finished: it is "
                    f"waiting on {what}. Its next turns will arrive here on their own; end your turn without a "
                    f"follow_up.")
            if task.get("key"):
                lead = f"Update from {profile} for {_label(task_id, task)}. Not finished: it is waiting on {what}."
            body = f"this turn failed: {failure}" if failure else text
            observer.deadline = time.monotonic() + self._follow_seconds()
            # Progress only, unless the turn failed: status, the session and the status bar show it.
            self._deliver(task_id, f"{lead}\n\n{body}", status="waiting_subwork", kind="update", tell=bool(failure),
                          last_update=text, pause_reasons=reasons, paused_at=time.time(), last_answer=None,
                          error=failure or None)
            return
        observer.deadline = None
        if failure:
            message = (f"Task {task_id} ({profile}) did not complete: {failure}\n"
                       f"The {profile} session {session_id} still exists.")
            self._deliver(task_id, message, status="failed", error=failure, pause_reasons=None)
        else:
            message = f"Answer from {profile} for task {task_id} (session {session_id}), finished:\n\n{text}"
            self._deliver(task_id, message, status="answered", last_answer=text, last_update=None,
                          pause_reasons=None, error=None)
        observer.stop()

    def _pause_reasons(self, task: dict, text: str) -> list[str]:
        """Why the task is not finished after this turn (empty: it is finished); see the README for the rule."""
        reasons = [f"task {tid} ({profile})" for tid, profile in self._open_subtasks(task)]
        if not any(m.startswith(LIVE_SESSION_INTERNALS) for m in self.soft_missing):
            session = self._live_session(task)
            if session is not None:
                if session.get("queued_prompt"):
                    reasons.append("a queued turn")
                if self._owns_background_command(session):
                    reasons.append("a background command")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and lines[-1].startswith("WAITING:"):
            reasons.append(lines[-1][len("WAITING:"):].strip() or "something it started")
        return reasons

    @staticmethod
    def _owns_background_command(session: dict) -> bool:
        try:
            from tui_gateway import session_notifications
            return bool(session_notifications._session_owns_pending_process_notification(session))
        except Exception:  # noqa: BLE001 - Hermes internals; the other conditions still apply
            logger.warning("profile-tasks: background commands of a session could not be read", exc_info=True)
            return False

    @staticmethod
    def _live_session(task: dict) -> dict | None:
        """The task session's record in this process's gateway, or None when this process does not hold it."""
        served = sys.modules.get("tui_gateway.server")
        if served is None or not hasattr(served, "_sessions"):
            return None
        with served._sessions_lock:
            session = served._sessions.get(task.get("session_id"))
            stored = task.get("stored_session_id")
            if session is None and stored:
                session = next((s for s in served._sessions.values()
                                if isinstance(s, dict) and s.get("session_key") == stored), None)
        return session if isinstance(session, dict) else None

    def _open_subtasks(self, task: dict) -> list[tuple[str, str]]:
        """(task id, profile) of the target profile's tasks started from this session that are not over.

        Not over: still starting, running, waiting or paused, or finished with the result not yet handed to the session.
        """
        ids = {i for i in (task.get("session_id"), task.get("stored_session_id")) if i}
        found = []
        for sub_id, sub in self._read_profile_tasks(task["profile"]).items():
            if not isinstance(sub, dict) or sub.get("caller_session_id") not in ids:
                continue
            if sub.get("status") in OPEN_SUBTASK_STATES or sub.get("delivered") is False:
                found.append((sub.get("task_id") or sub_id, sub.get("profile") or "?"))
        return found

    @staticmethod
    def _read_profile_tasks(profile: str) -> dict[str, dict]:
        """The tasks in *profile*'s plugin state, read under that profile's tasks.lock and never written."""
        from hermes_cli.plugins_state import PluginState
        folder = ProfileTasks._home(profile) / "plugin-data" / PluginState(PLUGIN_ID).data_dir.name
        try:
            try:
                lock = open(folder / "tasks.lock", "rb")
            except FileNotFoundError:
                # No writer ever took the lock: nothing can be half written.
                lock = contextlib.nullcontext()
            with lock:
                if hasattr(lock, "fileno"):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                data = json.loads((folder / "state.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("profile-tasks: the tasks of %s could not be read", profile, exc_info=True)
            return {}
        tasks = data.get(STATE_KEY) if isinstance(data, dict) else None
        return tasks if isinstance(tasks, dict) else {}

    def _follow_limit(self, task_id: str, observer: _Observer) -> None:
        """A paused task got no new turn for the follow limit: tell the manager, close the observer."""
        task = self.store.get(task_id) or {}
        if task.get("status") == "waiting_subwork":
            seconds = self._follow_seconds()
            span = f"{round(seconds / 3600, 1):g} h" if seconds >= 3600 else f"{seconds:g} s"
            what = ", ".join(task.get("pause_reasons") or ["something it started"])
            self._deliver(task_id, f"{_label(task_id, task)}: no news for {span}; it was "
                                   f"waiting on {what}; the session still exists; check with status",
                          status="follow_lost", kind="follow_limit", waiter=None)
        observer.stop()

    # approvals raised in a target session
    def _on_target_frame(self, task_id: str, frame: dict, turn: str | None = None) -> None:
        """Handle one frame of the target session; with *turn*, only while that turn still owns the task."""
        with self._turn_lock:
            task = self.store.get(task_id) or {}
            if turn is not None and task.get("turn_token") not in (None, turn):
                # A follow_up started a newer turn: this connection's late frames are the earlier turn's.
                return
            self._handle_frame(task_id, task, frame)

    def _handle_frame(self, task_id: str, task: dict, frame: dict) -> None:
        if frame.get("session_id") not in (None, task.get("session_id")):
            return
        payload = frame.get("payload") or {}
        kind = frame.get("type")
        if kind == "tool.complete" and (server := _reauth_server(payload)):
            self._mcp_auth_notice(task_id, server, "a tool call was refused: the server requires re-authentication")
        elif kind == "tool.complete" and str(payload.get("name") or "").startswith("mcp__"):
            self._check_mcp_tool_failure(task_id, task, payload)
        if kind == "approval.request" and payload.get("prompt_id"):
            self._approval_raised(task_id, payload)
        elif kind == "approval.settled" and payload.get("prompt_id"):
            self._approval_settled(task_id, payload)
        # On expiry the gated tool's result can arrive before approval.settled.
        elif kind == "tool.complete" and _approvals(task):
            self._call_completed(task_id, payload)
        elif kind == "message.complete":
            for prompt_id in _approvals(task):
                self._approval_ended(task_id, prompt_id, ("interrupted", ""))
        if kind == "tool.start" and payload.get("tool_id"):
            self._open_calls.setdefault(task_id, set()).add(str(payload["tool_id"]))
        elif kind == "tool.complete":
            self._open_calls.get(task_id, set()).discard(str(payload.get("tool_id") or ""))
        elif kind == "message.complete":
            self._open_calls.pop(task_id, None)

    def _call_completed(self, task_id: str, payload: dict) -> None:
        """End an open approval on its gated call's result only, not on a call running beside it.

        An approval names no tool call, so its gated one is among the calls running when it was raised
        (``calls``). A result blocked by an approval ends one approval that lists the call, a settled one first;
        a call that ran ends each approval listing it once no other call of its set is still running. An approval
        raised with no call running (no tool.start seen) ends on any result no listing approval claims. A result
        listed by more than one open approval cannot be told apart, so it ends them as ``unattributed``; each
        approval that listed it stays ``unattributed`` for the results that follow.
        """
        outcome = _approval_outcome(payload)
        tool_id = str(payload.get("tool_id") or "")
        ended: list[str] = []
        unattributed: set[str] = set()

        def take(task: dict) -> dict | None:
            approvals = _approvals(task)
            # A settled approval first: its gated call's result follows approval.settled.
            order = sorted(approvals, key=lambda p: approvals[p].get("stage") != "settling")
            named = [p for p in order if tool_id in (approvals[p].get("calls") or [])]
            shared = len(named) > 1
            # raised_calls keeps the set seen at raise time: a set drained by other results is not "no calls".
            unnamed = [p for p in order if not approvals[p].get("raised_calls", approvals[p].get("calls"))]
            for prompt_id in named:
                approvals[prompt_id] = {**approvals[prompt_id],
                                        "calls": [c for c in approvals[prompt_id]["calls"] if c != tool_id],
                                        "unattributed": shared or bool(approvals[prompt_id].get("unattributed"))}
            unattributed.clear()
            unattributed.update(p for p in approvals if approvals[p].get("unattributed"))
            if outcome[0] != "ran":
                ended[:] = (named or unnamed)[:1]
            else:
                ended[:] = [p for p in named if not approvals[p]["calls"]] if named else unnamed[:1]
            return _approval_fields(approvals) if named else None

        self.store.modify(task_id, take)
        for prompt_id in ended:
            ending = ("unattributed", outcome[1]) if prompt_id in unattributed else outcome
            self._approval_ended(task_id, prompt_id, ending)

    def _approval_raised(self, task_id: str, payload: dict) -> None:
        task = self.store.get(task_id) or {}
        profile, session_id = task.get("profile"), task.get("session_id")
        timeout_s = float(payload.get("timeout_s") or 0)
        raised = time.time()
        pending = {"prompt_id": payload["prompt_id"], "execution_generation": payload.get("execution_generation"),
                   "command": payload.get("command") or "", "description": payload.get("description") or "",
                   "choices": list(payload.get("choices") or ["once", "deny"]), "timeout_s": timeout_s,
                   "raised_at": raised, "expires_at": raised + timeout_s, "stage": "pending"}
        running = set(self._open_calls.get(task_id, ()))

        def add(t: dict) -> dict:
            approvals = _approvals(t)
            # A call is gated by one approval at most: the only call of an open approval is not this one's.
            taken = {a["calls"][0] for a in approvals.values() if len(a.get("calls") or []) == 1}
            calls = sorted(running - taken)
            # Hermes keeps one prompt per request: a second approval in the session sits beside the first.
            return _approval_fields({**approvals, pending["prompt_id"]: {**pending, "calls": calls,
                                                                         "raised_calls": calls}})

        self.store.modify(task_id, add)
        expires = time.strftime("%H:%M:%S", time.localtime(pending["expires_at"]))
        if task.get("key"):
            # A keyed caller has no conversation to relay the approval into.
            self._notify(task_id, (
                f"{_label(task_id, task)} is waiting for an approval in session {session_id}. It wants to run: "
                f"{pending['command']} ({pending['description']}). The request expires in {round(timeout_s)} s, "
                f"at {expires}; unanswered, the command does not run. It can be answered in the {profile} "
                f"session: {_session_ref(profile, session_id)}"), "approval")
            return
        self._notify(task_id, (
            f"Task {task_id} ({profile}) is waiting for an approval in session {session_id}. It wants to run: "
            f"{pending['command']} ({pending['description']}). "
            f"The request expires in {round(timeout_s)} s, at {expires}; unanswered, the command does not run. "
            f"To answer it here, call profile_task action=approval task_id={task_id}: the approve/deny prompt "
            f"opens in this conversation and the answer goes to the {profile} session. It can also be answered "
            f"in the {profile} session itself. To link that session in your reply, copy this reference verbatim, "
            f"with a space or a line end on both sides:\n{_session_ref(profile, session_id)}\n"), "approval")

    def _approval_settled(self, task_id: str, payload: dict) -> None:
        prompt_id = payload["prompt_id"]
        # Read under the relay lock: approval() marks the same approval relayed under it.
        with self._relay_lock:
            pending = _approvals(self.store.get(task_id) or {}).get(prompt_id)
            if not pending or pending.get("stage") != "pending":
                return
            relay = self._relays.get(prompt_id)
            # The manager's answer is in flight: approval() marks it relayed once the target confirms.
            forwarding = bool(relay and relay.get("forwarding"))
            forwarded = bool(relay and relay.get("forwarded")) or bool(pending.get("relayed"))
            # The settled frame names no outcome; the gated tool's result that follows it does.
            settling = {**pending, "stage": "settling", "settled_at": time.time(), "relayed": forwarded}
            self.store.modify(task_id, lambda t: _approval_fields({**_approvals(t), prompt_id: settling})
                              if prompt_id in _approvals(t) else None)
        if relay is not None and not forwarded and not forwarding:
            self._withdraw_manager_card(relay, "the request in the target session ended")

    def _approval_ended(self, task_id: str, prompt_id: str, outcome: tuple[str, str]) -> None:
        """Record how approval *prompt_id* really ended and tell the calling conversation, unless it answered it."""
        # Under the relay lock so a card answered now either forwards first or sees the approval gone.
        with self._relay_lock:
            task = self.store.get(task_id) or {}
            approval = _approvals(task).get(prompt_id)
            if not approval:
                return
            message, record = self._ended_text(task_id, task, approval, outcome)
            if self.store.modify(task_id, lambda t: {**_approval_fields(
                    {p: a for p, a in _approvals(t).items() if p != prompt_id}), "last_approval": record}
                    if prompt_id in _approvals(t) else None) is None:
                # A restart or a lost watch closed it since the read above; that path told the caller.
                return
            relay = self._relays.get(prompt_id)
            # Its answer is being forwarded: approval() sends this notice only if that forward fails.
            deferred = relay is not None and bool(relay.get("forwarding"))
            if deferred:
                relay["ended_notice"] = message
        if relay is not None and not relay.get("forwarded") and not deferred:
            self._withdraw_manager_card(relay, "the request in the target session ended")
        if approval.get("relayed") or deferred:
            return
        self._notify(task_id, message, "approval_end")

    @staticmethod
    def _ended_text(task_id: str, task: dict, approval: dict, outcome: tuple[str, str]) -> tuple[str, dict]:
        """The notice and the ``last_approval`` record of an approval that ended with *outcome*."""
        name, summary = outcome
        settled = approval.get("settled_at") or time.time()
        # An approval recorded without its raise time (e.g. by an older version) has no duration.
        raised = approval.get("raised_at")
        record = {**approval, "outcome": name, "user_summary": summary,
                  "settled_after_s": round(settled - raised, 1) if raised else None}
        after = f" after {record['settled_after_s']} s" if raised else ""
        profile, session_id = task.get("profile"), task.get("session_id")
        what = {
            "ran": f"was approved in the {profile} session; the command ran",
            "denied": f"was denied in the {profile} session; the command did not run",
            "timeout": (f"expired unanswered{after} (the {profile} profile's "
                        f"approvals.timeout is {round(approval.get('timeout_s') or 0)} s); the command did not run"),
            "cancelled": "was withdrawn before anyone answered (the turn was interrupted or corrected); the "
                         "command did not run",
            "interrupted": (f"ended with the {profile} turn before the command reported back; whether it ran is "
                            f"unknown: check the {profile} session before running it again"),
            "unattributed": (f"ended, but its result cannot be told apart from another approval open at the same "
                             f"time; whether it ran is unknown: check the {profile} session before running it "
                             f"again"),
        }.get(name, f"ended without running the command ({summary or name})")
        message = (f"{_label(task_id, task)}: the approval for `{approval.get('command')}` {what}. "
                   f"Session {session_id}: {_session_ref(profile, session_id)}")
        return message, record

    # MCP servers of the target profile that need a new sign-in
    async def _check_mcp_auth(self, conn: _Connection, task_id: str) -> None:
        """Read the target profile's MCP server states; notice each server that may need re-authentication.

        ``session.info`` carries them (``get_mcp_status`` in the session's profile); ``mcp.servers.status``
        is not served on this connection.
        """
        task = self.store.get(task_id) or {}
        try:
            info = await conn.rpc("session.info", session_id=task.get("session_id"))
        except Exception:  # noqa: BLE001 - the check must not end the task
            logger.warning("profile-tasks: MCP status of %s could not be read", task.get("profile"), exc_info=True)
            return
        for row in (info or {}).get("mcp_servers") or []:
            why = _mcp_auth_problem(row)
            if why and row.get("name"):
                self._mcp_auth_notice(task_id, str(row["name"]), why)
        try:
            runtime = _mcp_runtime(self._home(task["profile"]))
            set_aside = [(name, why) for name, (key, server) in runtime.items() if (why := _mcp_set_aside(key, server))]
        except Exception:  # noqa: BLE001 - Hermes internals; the check must not end the task
            logger.warning("profile-tasks: MCP runtime state of %s could not be read", task.get("profile"),
                           exc_info=True)
            return
        for name, why in set_aside:
            self._mcp_auth_notice(task_id, name, why)

    def _check_mcp_tool_failure(self, task_id: str, task: dict, payload: dict) -> None:
        """A failed MCP tool call (mcp__<server>__*) whose server's last HTTP error is a 401, or whose error text
        says so, gets the re-authentication notice."""
        error = _mcp_tool_error_text(payload)
        if not error:
            return
        name = str(payload["name"])
        noticed = task.get("mcp_auth_noticed") or []
        try:
            from tools.mcp_tool import _mcp_tool_server_names
            from tools.mcp_tool_schema import sanitize_mcp_name_component
            server = _mcp_tool_server_names.get(name)
            # Already noticed for this task: skip the locked read of every connection.
            if server in noticed or any(name.startswith(f"mcp__{sanitize_mcp_name_component(s)}__") for s in noticed):
                return
            runtime = _mcp_runtime(self._home(task["profile"]))
            if server not in runtime:
                # The generated name is lossy; match it against this profile's own servers.
                server = next((s for s in sorted(runtime, key=len, reverse=True)
                               if name.startswith(f"mcp__{sanitize_mcp_name_component(s)}__")), server)
            connection = runtime[server][1] if server in runtime else None
            rejected = connection is not None and _mcp_rejected_auth(connection)
            # Error text alone counts for a remote server only: a stdio server relays its own API's errors.
            remote = connection is not None and callable(getattr(connection, "_is_http", None)) and connection._is_http()
        except Exception:  # noqa: BLE001 - Hermes internals; the check must not end the task
            logger.warning("profile-tasks: MCP tool failure of %s could not be mapped", name, exc_info=True)
            return
        if rejected or (remote and _AUTH_TEXT.search(error)):
            self._mcp_auth_notice(task_id, server, "a tool call failed with HTTP 401 (unauthorized)")

    def _mcp_auth_notice(self, task_id: str, server: str, why: str) -> None:
        """One notice per server and task: which profile, which server, and the command that signs in again."""

        def mark(task: dict) -> dict | None:
            seen = list(task.get("mcp_auth_noticed") or [])
            return None if server in seen else {"mcp_auth_noticed": seen + [server]}

        with self._outbox_lock:
            first = self.store.modify(task_id, mark) is not None
        if not first:
            return
        task = self.store.get(task_id) or {}
        profile = task.get("profile")
        self._notify(task_id, (
            f"{_label(task_id, task)}: the MCP server '{server}' of the {profile} profile needs "
            f"re-authentication: {why}. Its tools may be unavailable to this task. To fix it, run: "
            f"hermes -p {profile} mcp login {server}"), "mcp_auth")

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
        waiting = [a for a in _approvals(task).values() if a.get("stage") == "pending"]
        if not waiting:
            last = task.get("last_approval")
            raise ValueError(f"task {task_id} has no pending approval"
                             + (f" (the last one ended: {last.get('outcome')})" if last else ""))
        live = [a for a in waiting if time.time() < a["expires_at"]]
        if not live:
            raise ValueError(f"the approval of task {task_id} has expired")
        from tools.approval import request_tool_approval
        from tools.approval_context import get_current_session_key
        manager_key = get_current_session_key()
        with self._relay_lock:
            # The oldest request with no card open: a second card for one request would take the first card's answer.
            pending = next((a for a in live if a["prompt_id"] not in self._relays), None)
            if pending is None:
                raise ValueError(f"the approval of task {task_id} is already open in a conversation; answer it there")
            profile, prompt_id = task["profile"], pending["prompt_id"]
            rule_key = f"{RELAY_RULE}:{prompt_id}"
            relay = {"manager_key": manager_key, "rule": f"plugin_rule:{rule_key}", "choice": None}
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
                still = (_approvals(self.store.get(task_id) or {}).get(prompt_id) or {}).get("stage") == "pending"
                return {"task_id": task_id, "forwarded": False, "manager_choice": choice,
                        "approved_here": bool(result.get("approved")),
                        "note": f"{note}. The request in the {profile} session is "
                                + ("still pending until " + expires if still else "no longer pending") + "."}
            if choice != "deny" and choice not in pending["choices"]:
                choice = "once"
            with self._relay_lock:
                current = _approvals(self.store.get(task_id) or {}).get(prompt_id) or {}
                relay["forwarding"] = current.get("stage") == "pending"
            # The target request ended as the answer came in: nothing is sent to the target session.
            answer = (self._run_async(self._respond(task, pending, choice)) if relay["forwarding"] else
                      {"ok": False, "error": "the approval in the target session had already ended"})
            # Marked forwarded only once the target took the answer: a failed forward keeps the outcome notice.
            with self._relay_lock:
                relay["forwarding"] = False
                ended_notice = relay.pop("ended_notice", None)
                if answer.get("ok"):
                    relay["forwarded"] = True
                    self.store.modify(task_id, lambda t: self._mark_relayed(t, prompt_id, choice))
            if ended_notice and not answer.get("ok"):
                self._notify(task_id, ended_notice, "approval_end")
        finally:
            with self._relay_lock:
                self._relays.pop(prompt_id, None)
        # forwarded stays False when the approval ended while the card was open.
        return {"task_id": task_id, "forwarded": bool(relay.get("forwarded")), "choice": choice,
                "target_answer": answer,
                "note": (f"Your {choice} was passed to the {profile} session; its final answer will arrive here."
                         if answer.get("ok") else f"The {profile} session did not take the answer: {answer}")}

    @staticmethod
    def _mark_relayed(task: dict, prompt_id: str, choice: str) -> dict | None:
        """Fields that mark the open or settling approval *prompt_id* as answered from the manager, or None."""
        approvals = _approvals(task)
        if prompt_id not in approvals:
            return None
        approvals[prompt_id] = {**approvals[prompt_id], "relayed": True, "relayed_choice": choice}
        return _approval_fields(approvals)

    async def _respond(self, task: dict, pending: dict, choice: str) -> dict:
        observer = self._live_observer(task["task_id"]) if task.get("task_id") else None
        if observer is not None and observer.loop is asyncio.get_running_loop():
            return await self._respond_on(observer.conn, task, pending, choice)
        if observer is not None:
            return await asyncio.wrap_future(observer.submit(
                lambda conn: self._respond_on(conn, task, pending, choice)))
        async with _Connection(self._home(task["profile"])) as conn:
            await conn.rpc("session.resume", session_id=task["session_id"], omit_messages=True)
            return await self._respond_on(conn, task, pending, choice)

    async def _respond_on(self, conn: Any, task: dict, pending: dict, choice: str) -> dict:
        try:
            result = await conn.rpc("approval.respond", session_id=task["session_id"],
                                    execution_generation=pending["execution_generation"],
                                    prompt_id=pending["prompt_id"], choice=choice)
        except Exception as exc:  # noqa: BLE001 - returned to the manager as the outcome
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        # The gateway answers {"status": "already_resolved"} without raising when the prompt had already ended.
        if not isinstance(result, dict) or result.get("status") != "resolved":
            return {"ok": False, "error": "the request had already ended in the target session", "result": result}
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
    def _notify(self, task_id: str, message: str, kind: str, trigger: str = "event", *, tell: bool = True) -> bool:
        """Queue *message* for the task's calling conversation and try to hand over the queue in order.

        With *tell* False the calling conversation gets nothing (progress only: status and the session show it).
        """
        if (self.store.get(task_id) or {}).get("key"):
            # A keyed task has no calling conversation: the notice is only recorded, never injected.
            entry = _notice(message, kind)
            with self._outbox_lock:
                self.store.modify(task_id, lambda task: {
                    "notices": _keyed_notices(task.get("notices") or [], [entry], trigger)})
            return True
        if not tell:
            logger.info("profile-tasks: task %s: %s notice not sent to the calling conversation", task_id, kind)
            # The event still retries notices queued earlier and refused.
            return self._flush_task(task_id, trigger)
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
            if task.get("key"):
                # Queued by a restart: a keyed task's notices are recorded, never injected.
                return not self.store.modify(task_id, lambda current: {
                    "outbox": [], "notices": _keyed_notices(current.get("notices") or [], current.get("outbox") or [],
                                                            trigger)})["outbox"]
            outbox = [dict(e) for e in task.get("outbox") or []]
            if not outbox:
                return True
            # One injection for the whole queue: Hermes may queue separate injections made at once in any order.
            try:
                ok = bool(self.ctx.inject_message("\n\n".join(e["text"] for e in outbox),
                                                  session_key=task.get("caller_session_key")))
            except Exception:  # noqa: BLE001 - a raising injection counts as a refusal
                logger.warning("profile-tasks: injection for task %s raised", task_id, exc_info=True)
                ok = False
            attempt = {"at": time.time(), "trigger": trigger, "ok": ok}
            for entry in outbox:
                entry["attempts"] = list(entry["attempts"]) + [attempt]
            sent: list[dict] = []
            fields: dict = {}
            if ok:
                sent = [{k: e[k] for k in ("id", "kind", "queued_at", "attempts")}
                        | {"delivered_at": attempt["at"], "trigger": trigger} for e in outbox]
                if any(e["kind"] == "result" for e in outbox):
                    fields.update(delivered=True, delivered_by="injection")
            else:
                logger.warning("profile-tasks: the calling conversation refused %d notice(s) of task %s (%s); kept "
                               "for the next try", len(outbox), task_id, ", ".join(e["kind"] for e in outbox))
            tried = {e["id"]: e for e in outbox}
            delivered = {e["id"] for e in sent}

            # Merged into the stored outbox: another process may have queued a notice during the injection.
            def merge(current: dict) -> dict:
                left = [tried.get(e["id"], e) for e in current.get("outbox") or [] if e["id"] not in delivered]
                return {"outbox": left, "notices": list(current.get("notices") or []) + sent, **fields}

            return not self.store.modify(task_id, merge)["outbox"]

    def _answer_taken_by_status(self, task_id: str, turn: str | None) -> bool:
        """Drop the queued result and mark an ended task delivered by status, in one write; True when marked.

        *turn* is the turn status read; a newer turn's result was not handed over, so it is left as it is.
        """
        marked = {}

        def change(task: dict) -> dict | None:
            if turn is not None and task.get("turn_token") != turn:
                return None
            fields = {"outbox": [e for e in task.get("outbox") or [] if e["kind"] != "result"]}
            if task.get("status") in ("answered", "failed") and not task.get("delivered"):
                # An injection already recorded keeps its delivered_by; a keyed task is handed over by result().
                fields.update(delivered=True, delivered_by="result" if task.get("key") else "status")
                marked["ok"] = True
            return fields

        with self._outbox_lock:
            self.store.modify(task_id, change)
        return bool(marked)

    def _drop_notices(self, task_id: str, kind: str) -> None:
        """Remove queued notices of *kind*; status uses _answer_taken_by_status for the result instead."""
        with self._outbox_lock:
            self.store.modify(task_id, lambda t: {"outbox": [e for e in t.get("outbox") or [] if e["kind"] != kind]})

    def _deliver(self, task_id: str, message: str, *, status: str, kind: str = "result", turn: str | None = None,
                 tell: bool = True, **fields: Any) -> None:
        """Record *status* and *fields* and send *message*; with *turn*, only while that turn still owns the task.

        *tell* False records without sending *message* to the calling conversation (see _notify).
        """
        new = {"status": status, **({"delivered": False} if kind == "result" else {}), **fields}
        final = status in ("answered", "failed")
        if final:
            # Nobody waits on a finished turn: a later load must not report it cut off by a restart.
            new["waiter"] = None
        told = {}
        # The end and its queued notice are one write: status then finds the notice to take, never an empty outbox.
        queued = final and kind == "result" and not (self.store.get(task_id) or {}).get("key")
        entry = _notice(message, kind)

        def change(task: dict) -> dict | None:
            if final and task.get("status") in ("answered", "failed") and task.get("delivered_by") == "status":
                # status already handed this end to the manager; checked under the same lock as status writes it.
                told["by_status"] = True
                return None
            if turn is not None and task.get("turn_token") != turn:
                return None
            return {**new, "outbox": list(task.get("outbox") or []) + [entry]} if queued else new

        with self._outbox_lock:
            owned = self.store.modify(task_id, change)
        if told:
            return
        if owned is not None and queued:
            self._flush_task(task_id, "event")
            return
        if owned is None:
            # A follow_up started a newer turn: its state stays; this notice is about the earlier turn only.
            logger.info("profile-tasks: task %s: a newer turn owns the task; its earlier %s is sent as a notice only",
                        task_id, kind)
            kind = f"earlier_{kind}"
        self._notify(task_id, message, kind, tell=tell)

    def _drop_dead_approval(self, task_id: str, cause: str) -> str:
        """Clear an approval nobody can watch any more; return a sentence for the notice, or ''."""
        task = self.store.get(task_id) or {}
        approvals = list(_approvals(task).values())
        if not approvals:
            return ""
        self.store.update(task_id, **_approval_fields({}),
                          last_approval={**approvals[-1], "outcome": "watch_lost", "user_summary": cause,
                                         "settled_at": time.time()})
        commands = ", ".join(f"`{a.get('command')}`" for a in approvals)
        return (f" Its approval request for {commands} can no longer be followed or answered from "
                f"here ({cause}); answer it in the {task.get('profile')} session if it is still open.")

    # gateway restart: found at load, told to each calling conversation
    def recover_after_restart(self) -> list[str]:
        """Close out tasks whose waiting process is gone; queue one notice each. Returns their ids."""
        found = [task_id for task_id in self.store.all()
                 if self.store.modify(task_id, lambda task, tid=task_id: self._close_cut_off(tid, task))]
        for task_id in list(self.store.all()):
            if self.store.modify(task_id, lambda task, tid=task_id: self._follow_again_fields(tid, task)):
                threading.Thread(target=self._follow_again, args=(task_id,), name=f"profile-task-follow-{task_id}",
                                 daemon=True).start()
        if any(task.get("outbox") for task in self.store.all().values()):
            threading.Thread(target=self._flush_when_hosted, name="profile-tasks-load", daemon=True).start()
        return found

    @staticmethod
    def _follow_again_fields(task_id: str, task: dict) -> dict | None:
        """Fields that hand a paused task whose process is gone to this process, with the restart notice queued."""
        if task.get("status") != "waiting_subwork" or not task.get("waiter") or _waiter_alive(task.get("waiter")):
            return None
        what = ", ".join(task.get("pause_reasons") or ["something it started"])
        text = f"{_label(task_id, task)}: the gateway restarted while task {task_id} was waiting on {what}; it is followed again."
        fields: dict = {"waiter": _process_token(os.getpid()), "paused_at": time.time()}
        approvals = _cut_approvals(task)
        if approvals:
            fields.update(_approval_fields({}))
        profile = task.get("profile")
        for approval in approvals:
            fields["last_approval"] = {**approval, "outcome": "interrupted", "settled_at": time.time()}
            text += (f" The approval for `{approval.get('command')}` ended with the restart: whether the command "
                     f"ran is unknown; check the {profile} session before running it again.")
        fields["outbox"] = list(task.get("outbox") or []) + [_notice(text, "restart")]
        return fields

    def _follow_again(self, task_id: str) -> None:
        """Give a paused task an observer again after a restart: it resumes the session."""
        try:
            observer = self._ensure_observer(task_id)
            observer.deadline = time.monotonic() + self._follow_seconds()
        except Exception as exc:  # noqa: BLE001 - reported to the manager as a lost wait
            logger.warning("profile-tasks: task %s could not be followed again", task_id, exc_info=True)
            self._wait_cut(task_id, None, f"{exc.__class__.__name__}: {exc}")

    def _retry_loop(self) -> None:
        """Try an undelivered notice again every retry_seconds while this plugin load lives."""
        while _PLUGIN is self:
            time.sleep(self.retry_seconds)
            try:
                if self._gateway_host():
                    self._flush_outbox("retry")
            except Exception:  # noqa: BLE001 - the next round tries again
                logger.warning("profile-tasks: the notice retry failed", exc_info=True)

    @staticmethod
    def _close_cut_off(task_id: str, task: dict) -> dict | None:
        """Fields that close a task cut off by a restart, its notice queued with them; None if it needs none.

        Read and written in one locked step, so of several processes loading the plugin at once only one does it.
        """
        if not task.get("waiter") or _waiter_alive(task.get("waiter")):
            return None
        if task.get("status") == "waiting_subwork":
            # A paused task is followed again by _follow_again_fields, which also ends its cut approvals.
            return None
        # A stop can cut the wait (wait_lost) or end the approval watch just before the process exits.
        waiting = task.get("status") in WAITING or task.get("status") == "wait_lost"
        approvals = _cut_approvals(task)
        if not waiting and not approvals:
            return None
        profile, session_id = task.get("profile"), task.get("session_id")
        keyed = bool(task.get("key"))
        parts = [f"{_label(task_id, task)}: the gateway restarted"]
        fields: dict = {"waiter": None, **_approval_fields({})}
        if waiting and not (session_id and task.get("admission_id")):
            fields.update(status="failed", error="the gateway restarted before the target accepted the task")
            parts.append(" before the target accepted it. " + ("Send it again." if keyed else "Start it again."))
        elif waiting and keyed:
            fields.update(status="wait_lost", error="the gateway restarted while the task was waiting")
            parts.append(f" while the {profile} turn was running; result() reads that turn's answer from the "
                         f"{profile} session {session_id}.")
        elif waiting:
            fields.update(status="wait_lost", error="the gateway restarted while the task was waiting")
            parts.append(f" while this task was waiting, so its answer will not be injected here. Check it "
                         f"with profile_task action=status task_id={task_id}; the {profile} session "
                         f"{session_id} still exists: {_session_ref(profile, session_id)}")
        else:
            parts.append(f" after the wait limit, while the {profile} session had an approval open.")
        for approval in approvals:
            fields["last_approval"] = {**approval, "outcome": "interrupted", "settled_at": time.time()}
            parts.append(f" The approval for `{approval.get('command')}` ended with the restart: whether the command "
                         f"ran is unknown; check the {profile} session before running it again.")
        fields["outbox"] = list(task.get("outbox") or []) + [_notice("".join(parts), "restart")]
        return fields

    def _queue_only(self, task_id: str, message: str, kind: str) -> None:
        with self._outbox_lock:
            entry = _notice(message, kind)
            self.store.modify(task_id, lambda task: {"outbox": list(task.get("outbox") or []) + [entry]})

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
        observer = self._live_observer(task["task_id"]) if task.get("task_id") else None
        if observer is not None:
            # A turn started by an injection or a queued prompt is known to the observer before the task records it.
            current = {**task, "admission_id": observer.turn_admission or task["admission_id"]}
            return await asyncio.wrap_future(observer.submit(lambda conn: self._steer_on(conn, current, text)))
        async with _Connection(self._home(task["profile"])) as conn:
            return await self._steer_on(conn, task, text)

    async def _steer_on(self, conn: Any, task: dict, text: str) -> Any:
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
        else:
            # Keyed tasks belong to their plugin caller, not to the tool's task list.
            tasks = {tid: t for tid, t in tasks.items() if not t.get("key")}
        extra: dict[str, dict] = {}
        # The turn each refreshed read is about; a follow_up may end a newer turn before the task is read again.
        refreshed_turn: dict[str, str | None] = {}
        for tid, task in tasks.items():
            if not _waiter_alive(task.get("waiter")) and (task.get("pending_approval")
                                                           or task.get("settling_approval")):
                self._drop_dead_approval(tid, "the process that watched the target session is gone")
                tasks[tid] = self.store.get(tid) or task
            followed = task.get("status") in (*WAITING, "waiting_subwork") and _waiter_alive(task.get("waiter"))
            if task.get("status") in REFRESHABLE and not followed:
                extra[tid] = self._refresh(task)
                refreshed_turn[tid] = task.get("turn_token")
                tasks[tid] = self.store.get(tid) or task
        keep = ("task_id", "profile", "status", "session_id", "title", "delivered", "error", "created_at", "updated_at")
        rows = []
        for tid, t in tasks.items():
            row = {k: t.get(k) for k in keep}
            row["link"] = _session_ref(t["profile"], t["session_id"]) if t.get("session_id") else None
            if t.get("pending_approval"):
                row["pending_approval"] = {k: t["pending_approval"].get(k) for k in
                                           ("command", "description", "expires_at")}
            if t.get("last_approval"):
                row["last_approval"] = {k: t["last_approval"].get(k) for k in ("command", "outcome", "user_summary")}
            if t.get("outbox"):
                row["notices_not_delivered"] = [e["text"] for e in t["outbox"]]
            if t.get("status") == "waiting_subwork" and t.get("last_update"):
                row["update"] = t["last_update"]
                row["waiting_on"] = t.get("pause_reasons")
            if task_id and t.get("last_answer") and "answer" not in extra.get(tid, {}):
                row["answer"] = t["last_answer"]
            if "answer" in row or "answer" in extra.get(tid, {}):
                # This result hands the answer over; the queued injection of it would repeat it.
                turn = refreshed_turn[tid] if "answer" in extra.get(tid, {}) else t.get("turn_token")
                if self._answer_taken_by_status(tid, turn):
                    row["delivered"] = True
                left = [e["text"] for e in t.get("outbox") or [] if e["kind"] != "result"]
                if left:
                    row["notices_not_delivered"] = left
                else:
                    row.pop("notices_not_delivered", None)
            rows.append(row | extra.get(tid, {}))
        return {"tasks": sorted(rows, key=lambda r: r.get("created_at") or 0), "targets": self.targets()}

    def _refresh(self, task: dict) -> dict:
        """Re-read the receipt of a task nobody waits for any more; record and return what it says."""
        task_id, profile, session_id = task["task_id"], task["profile"], task.get("session_id")
        # The turn this read is about: a follow_up may start a newer one while the receipt is read.
        turn = task.get("turn_token")
        newer = {"note": "A follow_up started a newer turn of this task while the earlier one was read; that read "
                         "is dropped. Check again with action=status."}
        restarted = task.get("status") in WAITING
        if restarted:
            restart_note = "The gateway restarted while this task was waiting, so no answer was injected. "
        elif task.get("status") == "wait_lost":
            restart_note = (f"The plugin stopped waiting for {profile} ({task.get('error')}), "
                            f"so no answer was injected. ")
        else:
            restart_note = ""
        if task.get("status") == "follow_lost" or (task.get("status") == "wait_lost" and task.get("pause_reasons")):
            # Nobody follows the paused session any more: read its latest turn, not the paused turn's receipt.
            return self._refresh_lost(task, turn)
        if not session_id or not task.get("admission_id"):
            error = "the gateway restarted before the target accepted the task" if restarted else "not accepted yet"
            if restarted and not self._record(task_id, turn, status="failed", error=error):
                return newer
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
            if restarted and not self._record(task_id, turn, status="wait_lost"):
                return newer
            return {"note": f"{restart_note}Could not read the turn's state from {profile}: "
                            f"{box.get('error', 'timed out')}. The {profile} session {session_id} still exists."}
        state = receipt.get("status")
        if state not in ("terminal", "unknown"):
            if ((restarted or task.get("status") == "wait_lost")
                    and not self._record(task_id, turn, status="waiting_timeout")):
                return newer
            return {"turn": state, "note": f"{restart_note}{profile} is still on it (turn {state}). The work is "
                                            f"not cancelled. Check back later with action=status."}
        ok, body = _final_text(receipt)
        reasons = self._pause_reasons(task, body if ok else "")
        if reasons:
            # Not the real end: an update only; the observer, or a later status, takes the next turns.
            return {"turn": state, "update": body, "waiting_on": reasons,
                    "note": f"{restart_note}{profile} is not finished: it is waiting on {', '.join(reasons)}."}
        if ok:
            closed = (self.store.get(task_id) or {}).get("status") in ("answered", "failed")
            if closed or not self._record(task_id, turn, open_only=True, status="answered", last_answer=body,
                                          delivered=True, delivered_by="status"):
                if (self.store.get(task_id) or {}).get("status") in ("answered", "failed"):
                    # The observer ended the task while the receipt was read: its notice carries the answer.
                    return {}
                return newer
            return {"turn": state, "answer": body,
                    "note": f"{restart_note}Answer read from the {profile} session after the wait ended."}
        if not self._record(task_id, turn, open_only=True, status="failed", error=body, delivered=True,
                            delivered_by="status"):
            if (self.store.get(task_id) or {}).get("status") in ("answered", "failed"):
                # The observer ended the task while the receipt was read: its notice carries the end.
                return {}
            return newer
        if state == "unknown":
            return {"turn": state, "note": (
                f"{restart_note}Hermes marked the turn unknown: the gateway stopped while it ran, so it did "
                f"not finish ({body}). The {profile} session {session_id} is paused on that turn until a person "
                f"resends or discards it in the desktop; a follow_up queues behind it until then.")}
        return {"turn": state, "note": f"{restart_note}The turn did not complete: {body}. The {profile} "
                                       f"session {session_id} still exists."}

    def _refresh_lost(self, task: dict, turn: str | None) -> dict:
        """A task given up at the follow limit: say whether its session still runs and what its sub-tasks do."""
        task_id, profile, session_id = task["task_id"], task["profile"], task["session_id"]
        box: dict = {}

        async def read() -> None:
            async with _Connection(self._home(profile)) as conn:
                box["info"] = await conn.rpc("session.info", session_id=session_id)
                if not (box["info"] or {}).get("running"):
                    box["resumed"] = await conn.rpc("session.resume", session_id=session_id)

        try:
            self._run_blocking(read())
        except Exception as exc:  # noqa: BLE001 - reported to the manager as the status
            return {"note": f"Could not read the {profile} session {session_id}: {exc.__class__.__name__}: {exc}."}
        info = box.get("info") if isinstance(box.get("info"), dict) else {}
        subtasks = [{"task_id": tid, "profile": p} for tid, p in self._open_subtasks(task)]
        out: dict = {"waiting_on_subtasks": subtasks} if subtasks else {}
        resumed = box.get("resumed") if isinstance(box.get("resumed"), dict) else {}
        last = next((m for m in reversed(resumed.get("messages") or [])
                     if isinstance(m, dict) and m.get("role") == "assistant"), None)
        body = str((last or {}).get("text") or (last or {}).get("content") or "")
        stamp = (last or {}).get("timestamp")
        # A turn is new when written after the last one told; the text decides without a timestamp or told time.
        newer = (stamp > task["told_at"] if isinstance(stamp, (int, float)) and task.get("told_at")
                 else body != task.get("told_text"))
        if info.get("running") is False and body.strip() and newer:
            reasons = self._pause_reasons(task, body)
            if reasons:
                # Still not the real end: told once as an update, the task stays past its follow limit.
                if self._record(task_id, turn, open_only=True, last_update=body, pause_reasons=reasons,
                                told_text=body, told_at=stamp or time.time()):
                    return {**out, "update": body, "waiting_on": reasons, "note": "A later turn of the session ended "
                            f"after the follow limit; it is still waiting on {', '.join(reasons)}."}
            elif self._record(task_id, turn, status="answered", last_answer=body, delivered=True,
                              delivered_by="status", told_text=body, told_at=stamp or time.time()):
                return {**out, "answer": body, "note": "A later turn of the session ended after the follow "
                        "limit; this is its last answer."}
        if info.get("running"):
            return {**out, "note": f"The {profile} session {session_id} is still running a turn."}
        return {**out, "note": f"The {profile} session {session_id} is idle; no turn ended since the follow limit."}

    @staticmethod
    def _run_blocking(coro: Any) -> None:
        box: dict = {}

        def run() -> None:
            try:
                asyncio.run(coro)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        thread = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True)
        thread.start()
        thread.join(ACK_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise TimeoutError(f"no reply in {ACK_TIMEOUT_SECONDS}s")
        if "error" in box:
            raise box["error"]

    def _record(self, task_id: str, turn: str | None, open_only: bool = False, **fields: Any) -> bool:
        """Store *fields* only while *turn* still owns the task; False when a follow_up started a newer turn.

        With *open_only*, also False when the task is already answered or failed.
        """
        if fields.get("status") in ("answered", "failed"):
            fields["waiter"] = None

        def change(task: dict) -> dict | None:
            if task.get("turn_token") != turn:
                return None
            return None if open_only and task.get("status") in ("answered", "failed") else fields

        return self.store.modify(task_id, change) is not None

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
        "new message in the same task session once its turn has ended (refused while it runs). A task is followed "
        "to its real end: while it waits on its own work or runs past the wait limit nothing arrives here "
        "(action=status shows its progress), and the last message says finished. Failures, approvals and "
        "restarts still arrive here. action=correct "
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


# The plugin registered in this process; send() and result() go through it.
_PLUGIN: ProfileTasks | None = None


def _loaded() -> ProfileTasks:
    if _PLUGIN is None:
        raise RuntimeError("profile-tasks is not loaded in this process")
    if _PLUGIN.missing:
        raise RuntimeError("profile_task is unavailable: this Hermes no longer has the internals it depends on: "
                           + "; ".join(_PLUGIN.missing))
    return _PLUGIN


def send(key: str, profile: str, text: str, title: str | None = None) -> dict:
    """Send *text* to *profile* in the session of *key*, for a caller with no conversation (a plugin backend).

    The first call for a key creates its task and session (visible in the chat list); later calls are follow-ups
    in that same session. Returns once the target accepted the text (up to 120 s; run it off the event loop):
    ``key``, ``task_id``, ``accepted`` and, when accepted, ``session_id``, ``session_title`` and ``link``.
    Raises ValueError for a refused call: unknown profile, empty text, another profile for the key, or a turn
    of the key still running. Nothing is injected into any conversation; read the answer with ``result``.
    """
    return _loaded().send_keyed(key, profile, text, title)


def result(key: str, wait: float = 0.0) -> dict:
    """The state of *key*'s task: ``status`` (``starting``/``running`` while its turn runs, then ``answered``,
    ``failed``, ``waiting_timeout`` or ``wait_lost``), ``answer`` (the last turn's final text, once answered),
    ``error``, ``pending_approval``, ``task_id``, ``session_id``, ``link``, and ``notices`` (wait limit,
    approvals, MCP sign-in, restart: ``kind``, ``text``, ``queued_at``) when there are any. With *wait*, blocks up
    to that many seconds while the turn runs. Raises ValueError for an unknown key or a non-numeric *wait*.
    """
    plugin = _loaded()
    try:
        seconds = float(wait)
    except (TypeError, ValueError):
        raise ValueError(f"wait must be a number of seconds, not {wait!r}") from None
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        row = plugin.keyed_result(key)
        remaining = deadline - time.monotonic()
        if row.get("status") not in WAITING or remaining <= 0:
            return row
        time.sleep(min(1.0, remaining))


def register(ctx: Any) -> None:
    global _PLUGIN
    missing = check_internals(ctx)
    soft = [m for m in missing if m.startswith(LIVE_SESSION_INTERNALS + STOP_INTERNALS)]
    missing = [m for m in missing if m not in soft]
    pause = [m for m in soft if m.startswith(LIVE_SESSION_INTERNALS)]
    if pause:
        logger.error("profile-tasks: Hermes internals missing after an update; a task is only held open for the "
                     "tasks of its profile and a WAITING: line: %s", "; ".join(pause))
    if len(pause) < len(soft):
        logger.error("profile-tasks: Hermes internals missing after an update; a turn cut by a gateway stop is "
                     "reported failed: %s", "; ".join(m for m in soft if m not in pause))
    if missing:
        logger.error("profile-tasks: Hermes internals missing after an update; the %s tool will refuse every "
                     "call until the plugin is fixed: %s", TOOL_NAME, "; ".join(missing))
    if _PLUGIN is not None:
        logger.warning("profile-tasks: registered again in this process; send() and result() now use the new load")
    plugin = _PLUGIN = ProfileTasks(ctx, missing)
    plugin.soft_missing = soft

    def _forget() -> None:
        global _PLUGIN
        if _PLUGIN is plugin:
            _PLUGIN = None

    if hasattr(ctx, "on_unload"):
        ctx.on_unload(_forget)
    if not missing:
        plugin._retry_thread = threading.Thread(target=plugin._retry_loop, name="profile-tasks-retry", daemon=True)
        plugin._retry_thread.start()
        recovered = plugin.recover_after_restart()
        if recovered:
            logger.info("profile-tasks: tasks cut off by a gateway restart: %s", ", ".join(recovered))
    ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=SCHEMA, handler=plugin.handle,
                      description=SCHEMA["description"], emoji="📨")
    ctx.register_hook("post_approval_response", plugin.on_approval_response)
