# profile-tasks (v0.1)

Hermes plugin for the manager (default profile). Its `profile_task` tool hands a task to another
profile served by the same Hermes gateway.

- `start`: creates one session under the target profile, the way the desktop does
  (`session.create` with `source="gui"`, titled `Task <id>: <title>`), then submits the text
  (`prompt.submit`). Returns at once: task id, target profile, session id, title and a
  `link`, the desktop's session reference `@session:<profile>/<session id>`. The desktop renders it
  as a clickable chip (in a user message) or link (in a model reply) that opens the session on its
  profile, only when it stands alone: whitespace or a line end on both sides, no punctuation or
  parenthesis glued to it.
- When the target's turn ends, its final text is injected once into the calling manager
  conversation (`ctx.inject_message`), which starts a manager turn.
- `follow_up`: sends a new text to the same session (same context). Refused while the task's turn
  is still running (or its state cannot be read): use `correct` for that turn, or send the
  follow-up after its answer arrives.
- `correct`: steers the running turn of the task (`prompt.receipt` then `session.steer`).
- `status`: lists the tasks from the plugin state and the allowed targets. For a task nobody waits on
  any more (past the wait limit, a wait cut off, or a gateway restart, detected by the waiting
  process being gone) it reads the target turn's receipt. A late answer comes back in the status
  result and is never injected; later status calls for that task return the stored answer without
  reading the target again. A turn cut off by the restart is reported with its state
  (`interrupted`, or `unknown`, in which case the target session stays paused until a person
  resends or discards the turn in the desktop). Status also lists, per task, the last approval's
  outcome and any notice the manager conversation has not taken yet.
- Notices to the manager conversation (answers, wait limit, approvals, restarts) are kept in the
  plugin state until that conversation takes them. All the notices a task has queued go in one
  injection, in order: Hermes may queue separate injections made at once in any order. Notices of
  different tasks are still separate injections. A refused one is retried, in order, at the start
  of the next `profile_task` call or at the next event of its task; the gateway refuses a
  conversation that no client has open.
- Gateway restart: when the plugin loads, it finds the tasks whose waiting process is gone, marks
  them (`wait_lost`, or `failed` if the target never accepted the task), closes any approval that
  was open (whether the command ran is unknown), and queues one notice per task to its manager conversation.
  It sends them once the gateway accepts plugin messages, then as above. When several processes load
  the plugin at once (a gateway launcher that then attaches to the running gateway, and that gateway),
  each task is closed and noticed by one of them only: the plugin state is read and written under a
  lock file in the plugin's data folder.
- A task is followed to its real end. An observer per open task (its own connection to the target
  profile's gateway, bound to the task session) reads every event of the session. Every turn that
  ends there, whoever started it (an injection, a queued prompt, a finished background command), is
  told to the manager. After each turn the plugin decides finished or paused; the task is paused
  if any of four conditions holds:
  1. a task of the target profile, started from this session (`caller_session_id`), is still
     `starting`, `running`, `waiting_timeout` or `waiting_subwork`, or finished with its result not
     delivered yet (read from that profile's plugin state, read-only, under its `tasks.lock`);
  2. a prompt is queued in the live session;
  3. a background command of the session will notify it when it ends
     (`_session_owns_pending_process_notification`);
  4. the last non-empty line of the turn's text starts with `WAITING:` (the target is told to write
     it when something it started will bring it back; without it the answer is taken as final).
  Paused: the manager gets `Update from <profile> for task <id> ... Not finished: it is waiting on
  <reasons>` with the turn's text, the task is `waiting_subwork`, and its next turns arrive on their
  own (no `follow_up` needed; one is accepted, and sent on the observer's connection). Finished: the
  manager gets `Answer from <profile> ... finished:`, the task is `answered` and the observer closes.
  A failed turn is an update saying so while something is open, else the task fails as before.
  Conditions 2 and 3 use Hermes internals that exist only in the gateway process; if they are
  missing, the load reports it and only conditions 1 and 4 apply.
- Follow limit: `follow_timeout_seconds` (default 86400). A paused task with no new turn that long
  gets a `follow_limit` notice, becomes `follow_lost` and its observer closes; the session still
  exists, and `status` reads it (a turn that arrived later is returned as a late answer).
- Wait limit: 30 minutes by default, for the turn the plugin submitted. Past it the manager gets a message: no answer yet, the work
  is not cancelled, check back with `status`.
- Approvals in a task session: the plugin's connection to the target session sees its
  `approval.request` at once and injects a notice into the manager conversation (command, task,
  session, its `@session:<profile>/<session>` reference on a line of its own with the instruction to
  copy it verbatim, expiry time from the target profile's `approvals.timeout`). The approval ending
  and restart notices end with the same reference. When the request ends, a second notice gives the
  real outcome, read from the gated tool's result (its approval summary, or the `BLOCKED:` refusal of
  a file-write gate such as a protected `SOUL.md`): approved and run, denied,
  expired, withdrawn, or ended with the turn. The approval names no tool call, so the gated call is
  taken among the target's calls running when it was raised: a result blocked by the approval ends
  it at once; a call that ran ends it once no other call of that set is still running. A call
  started after the approval does not end it, and a call that is the only one an open approval
  lists is not counted for a later approval. When two approvals open over the same running calls,
  a result listed by both cannot be told apart: the notice says whether it ran is unknown and to
  check the target session, instead of naming an outcome. Each of those approvals keeps that
  unknown outcome for the results that follow. `approval` (action) shows the
  approve/deny prompt in the manager conversation itself; the choice made there (`once`,
  `session`, `always`, `deny`) is sent to the target session with `approval.respond`, so the
  command runs there. `always`
  permanently allows that kind of command in the target profile (its `command_allowlist`) and leaves
  one inert rule in the manager profile's config (`plugin_rule:profile_task_relay:<prompt id>`, one
  per relayed prompt, never matched again); the prompt's own text says so. Limits: the prompt opens
  only when the manager turn calls `approval` (a plugin cannot open an approval prompt in a
  conversation with no running turn); its card title is the generic plugin rule, the target command
  is in its description; it uses the manager profile's approval timeout, and if the manager's
  approvals are bypassed (yolo or mode off) nothing is passed on and the target request stays
  pending. If the target request ends (expired, answered in the target session, turn interrupted)
  while the card is open, the card is withdrawn and the manager is told the real ending; an answer
  that reaches the plugin after that ending is not recorded as pending nor sent to the target
  session. After the wait limit the plugin keeps watching the target turn (up to 24 h) only to relay
  approvals.
- MCP servers needing a new sign-in: at the start and at the end of every turn of a task (past the
  wait limit, when the approval watch ends), the plugin reads the target session's MCP server states
  (`session.info`, `mcp_servers`). A server whose
  sign-in was rejected (`needs_auth`), or a remote (non-stdio) server that failed to connect, gets a
  notice in the manager conversation: task, profile, server, and the command that fixes it
  (`hermes -p <profile> mcp login <server>`). A target tool result carrying `needs_reauth: true`
  (`tool.complete`) gets the same notice. One notice per server per task; it goes through the
  notice queue above.
- A 401 on an MCP tool call mid-task: this Hermes returns a generic `MCPError: Server returned an
  error response` without `needs_reauth`, but records the HTTP status on the connection
  (`_http_rejection`). On a failed `tool.complete` of an `mcp__<server>__*` tool, the plugin maps
  the tool to its server (Hermes' tool-to-server map, else the target profile's server names) and
  sends the notice when that connection's last HTTP error is a 401, or, for a remote server, when
  the error text says 401 / unauthorized / invalid token (a failed result without an `error` key
  is read as its whole JSON body). A stdio server's error text is not
  trusted: it may relay its own API's 401. The error-text fallback has a known false positive: a
  hosted (remote) MCP server that relays a third-party API's 401 in its error text gets a notice
  while its own connection is fine.
- A server Hermes set aside: after 3 failed calls Hermes opens a circuit breaker and refuses that
  server's calls for 60 s, while `session.info` still shows it `connected` with no `needs_auth`.
  A server whose reconnects are exhausted is parked (`failed` in `session.info`). At each state
  read the plugin also reads the target profile's connections in the gateway process and sends
  the notice for one whose breaker is open or that is parked, when its last HTTP error is a 401.
  A breaker opened by a 500 gets no notice. A breaker opened by timeouts or connection resets can
  give an unneeded notice: Hermes clears the stored last HTTP error (`_http_rejection`) only when it
  opens a new HTTP connection, so a 401 recorded during an OAuth sign-in or token refresh stays on
  record, and later timeouts or resets on that connection are read as a 401.
- Connections not keyed by (profile home, server name), such as a session tool scope's, are
  skipped (debug log).
- These two checks read Hermes internals: in `tools.mcp_tool`, `_servers`, `_lock`,
  `_server_error_counts`, `_server_breaker_opened_at`, `_CIRCUIT_BREAKER_THRESHOLD`,
  `_CIRCUIT_BREAKER_COOLDOWN_SEC` and `_mcp_tool_server_names`; on a connection, `_http_rejection`,
  `_park_reason`, `session` and `_is_http()`; and `tools.mcp_tool_schema.sanitize_mcp_name_component`.
  If they change, the check logs a warning and the task goes on; it does not fail the load check.
- Targets: every profile the gateway serves (`gateway_state.json` `served_profiles`) except the caller.
- The target runs with its own config, MCP servers and tokens. The manager only gets text back.
- At load time the plugin checks the Hermes internals it uses, including the plugin context's
  `_manager`. If an update removed one, it logs an error naming it and the tool refuses every call
  with that message.

## Desktop status-bar item

The package also has a desktop part (`desktop/plugin.js`) and a read-only backend route
(`dashboard/plugin_api.py`), installed with the plugin.

- In the status bar (right side), a speech bubble and the number of `profile_task` tasks started from
  the conversation open on screen. The item is hidden when that conversation started none, and it
  changes with the open conversation. It turns yellow while a task waits for an approval.
- Clicking it lists those tasks, newest first: target profile, title and state: running, waiting
  for an approval (with the command), past the wait limit, wait lost, answered, late answer (read
  with `status` after the wait ended) or failed. Clicking a task opens its session in a tab, as the
  `@session:<profile>/<id>` chip does.
- Each task records its calling conversation's session id (`caller_session_id`, the desktop's
  stored session id) at `start` and on a `follow_up` from another conversation. Tasks started before
  this field existed and keyed tasks are not listed.
- `GET /api/plugins/profile-tasks/tasks?session_id=<id>&session_profile=<profile>` returns that
  list from the named profile's plugin state file as it is on disk: no write, no lock, no call to
  a target (unlike `action=status`, which refreshes and records). The item reads it again when the
  open conversation changes, on the `plugin.profile-tasks.tasks.changed` event the gateway sends on
  every task save, and every 60 s.
- By design the route reads the named profile's state, across the profile boundary: a row only
  comes back to a caller that already knows that conversation's session id.

## From a plugin backend (no calling conversation)

Code running in the gateway process (a dashboard plugin backend, for example) calls the module the
gateway loaded, `sys.modules["hermes_plugins.profile_tasks"]`:

- `send(key, profile, text, title=None)`: the first call for a key creates a task and its session in
  `profile` (a normal session, listed in that profile's chat list); later calls on the same key are
  follow-ups in that session. Returns once the target accepted the text (up to 120 s; call it off the
  event loop): `key`, `task_id`, `accepted`, `session_id`, `session_title`, `link`. Raises
  `ValueError` for an unknown profile, empty key or text, another profile than the key's, or a turn
  of the key still running. A key whose task never got a session starts a new task.
- `result(key, wait=0)`: `status` (`starting`/`running`, then `answered`, `failed`,
  `waiting_timeout` or `wait_lost`), `answer` (the last turn's final text, once answered), `error`,
  `pending_approval` (the oldest one still waiting when the target session has several), `task_id`,
  `session_id`, `link`, and `notices` when there are any: the
  wait limit, approval, MCP sign-in and restart notices (`kind`, `text`, `queued_at`). `wait` blocks
  up to that many seconds while the turn runs; a non-numeric `wait` raises `ValueError`. Past the
  wait limit or after a restart it reads the target turn, like `status`. A second `send` on a key
  whose first one is still being submitted raises `ValueError` (turn still running).

Nothing is injected into any conversation for a keyed task: its notices (answer, approval, wait
limit, MCP sign-in, restart) are only recorded on the task, the latest 20, the answer's without its
text (it is the `answer`); an accepted follow-up clears those of the earlier turns. A keyed task's
notices name it by key, never by task id, never tell it to call `profile_task`, and point to
`result()` (wait limit, cut-off wait, restart); an approval in its session can only be answered in
that session, whose `@session:` reference the notice gives. Unloading the plugin clears what
`send`/`result` use; they then raise until it is loaded again. Keyed tasks are left out of the tool's
`action=status` list. Keys and tasks are kept in the plugin state like the tool's.

## Enable

Tested against Hermes Agent v0.23.3 (commit `caafe5389ae5`) and v0.23.2 (commit `ae2a81ccc00e`).
Supported on Linux and macOS: the waiting-process check reads `/proc` on Linux and uses `psutil` (a
Hermes dependency) on macOS. The tests run on Linux; they cover the macOS path by hiding `/proc`.
Linux only in the e2e harness: the remote MCP fixture also dies with the harness process
(`PR_SET_PDEATHSIG`); elsewhere the harness stops it on exit only.

1. Install and enable the plugin in the default profile:

   ```bash
   hermes plugins install fneuch/hermes-profile-tasks/profile-tasks --enable
   ```

   Only this folder is installed, its desktop part and backend route included. The app copies the
   desktop part when it reloads: after the install, restart the gateway and reload the app (Ctrl+R).
   In the desktop, Skills > Plugins > Install from Git takes
   `fneuch/hermes-profile-tasks#profile-tasks`.

2. In `~/.hermes/config.yaml`:

   ```yaml
   plugins:
     entries:
       profile-tasks:
         allow_gateway_injection: true   # lets the plugin inject the answer into the manager session
         settings:
           wait_timeout_seconds: 1800    # optional, default 1800
           follow_timeout_seconds: 86400 # optional, default 24 h: a paused task is no longer followed
   ```

3. The gateway must serve the target profiles (`gateway.multiplex_profiles: true`, already the case
   when the desktop shows several profiles).
4. Restart the gateway, then reload the app (Ctrl+R).

Nothing is needed in the target profiles.

Update with `hermes plugins update profile-tasks`, then restart the gateway and reload the app (Ctrl+R).

## Tests

Both run against an isolated Hermes home under `~/.cache/profile-tasks-e2e/<timestamp>/`
(fake `HOME`, own gateway, lock dir and runtime dir, fake LLM providers); the live install is
only read (code and interpreter).

- `PT_SCRIPT=tests/test_load_check.py e2e/run.sh`: load-time check, with all internals and with one removed.
- `PT_SCRIPT=tests/test_mcp_reauth.py e2e/run.sh`: the `needs_reauth` tool-result notice (one per
  server and task, kept when the manager conversation refuses it, approvals unaffected), the
  generic-error 401 mapping (no notice for a 500, a stdio server, a non-MCP tool; no connection
  read for a server already noticed), the skipped session-scope connection key, and the
  set-aside reading (breaker open or parked after a 401; not after a 500 or past the cooldown).
- `PT_SCRIPT=tests/test_keyed.py e2e/run.sh`: `send`/`result` by key with a fake target: one task and
  session per key, follow-up in the same session, answer returned and injected nowhere, refused
  calls (including a non-numeric `wait` and a send while the key's first send is being submitted),
  wait-limit and MCP sign-in notices in `result`, notices capped and cleared by a follow-up, keyed
  tasks out of the tool's status list, a neutral restart notice recorded only, no keyed notice
  text naming `profile_task` or a task id, unload.
- `PT_SCRIPT=tests/test_turn_races.py e2e/run.sh`: turn and approval races with a fake target: two
  follow-ups racing start one turn, a follow-up clears the previous answer and error, an earlier
  turn ending late leaves the newer turn's state, an interrupted approval reports an unknown
  outcome, an approval answer counts as forwarded only once the target took it, a call running
  beside the gated one does not end its approval, a status read of an earlier turn leaves a newer
  turn's state, frames queued before the terminal receipt still reach the plugin, an approval the
  target had already ended is not reported as forwarded, two approvals open in one session each
  end on their own call, a failure after the 120 s acknowledgement wait is recorded, a new turn
  drops the earlier turn's open calls, a finished task is not reported cut off at the next load.
- `PT_SCRIPT=tests/test_follow_real_end.py e2e/run.sh`: a task followed to its real end with a fake
  gateway: an injected turn reaches the manager, the four-condition rule (sub-task of the session,
  queued prompt, background command, `WAITING:` last line), failed turns, `follow_up` and approval
  answers on the observer's connection, the follow limit, restart, the 60 s notice retry, `result(key)`.
- `PT_SCRIPT=tests/test_status_item.py e2e/run.sh`: the status-bar item's data: `caller_session_id`
  set at `start`, moved by a `follow_up` from another conversation, none on a keyed task; each
  stored status mapped to the item's state; `GET /tasks` lists only the conversation's own tasks
  from the named profile's state file and leaves that folder byte for byte as it was; a task save
  sends `plugin.profile-tasks.tasks.changed` only where the gateway server runs.
- `PT_SCRIPT=tests/test_liveness.py e2e/run.sh`: the waiting-process check with `/proc` (Linux) and
  with `/proc` hidden (the macOS path): a live waiter, a reused pid, a gone pid, a zombie.
- `PT_SCRIPT=tests/test_e2e_harness.py e2e/run.sh`: the probe plugin's response is never read half
  written; `e2e/stop_gateway.py` reads a gateway that exits while it is read as stopped.
- `e2e/run.sh [trial ...]`: the end-to-end trials (`1-2-3 4 5 6 7 8 9 limit targets restart followup
  retry mcp_auth keyed approval`; with no argument all but `9`, which needs the desktop app and runs
  only when named; `keyed` calls `send`/`result` from a gateway thread of the
  `e2e/probe-plugin` plugin, with no conversation; `approval` runs last because its relayed `always` permanently allows recursive
  delete in the isolated analyst profile). Evidence is written to `e2e/evidence/run-<timestamp>.json`. The analyst profile gets
  a fixture MCP server named `atlassian` (from the Hermes test fixtures) whose answer carries a
  per-run token set only in the analyst config. The default (manager) profile gets MCP servers
  like the live one: an enabled fixture server named `notion` and a disabled `atlassian` entry. The
  implementer profile gets two remote (HTTP) MCP servers that need a new sign-in: `jira` answers
  401 to every request, `linear` (the Hermes HTTP fixture) connects but answers 401 to every tool
  call. Trial `mcp_auth` checks the notices they produce: `jira` at task start, `linear` on its
  first 401 mid-task, nothing more for two more 401s in a second turn, then `linear` again for a
  new task that calls nothing, because Hermes' breaker is open.

`e2e/run.sh` stops the isolated gateway on every exit (end, failure, Ctrl-C, TERM) through that
home's own control socket (`e2e/stop_gateway.py HERMES_HOME`, which refuses the real `~/.hermes`).

Trial 9 (clicking the link in the desktop) is not covered: the isolated run has no desktop app.

## Known issues

- `inject_message` returns True once the message is scheduled, before it is queued for the session;
  separate injections made at once can reach the session queue in any order. This made the e2e
  trial `retry` flaky: its three kept notices, handed over in three injections, could queue the
  approval notice last, still unread when the run stopped the gateway. Fixed by injecting a task's
  queued notices together (10 standalone runs of `retry` in a row passed). Notices of different
  tasks, or of events far enough apart, are still separate injections, and an injection refused
  after scheduling is not seen by the plugin.
