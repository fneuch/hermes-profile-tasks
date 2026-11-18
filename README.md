# profile-tasks (v0.1)

Hermes plugin for the manager (default profile). Its `profile_task` tool hands a task to another
profile served by the same Hermes gateway.

- `start`: creates one session under the target profile, the way the desktop does
  (`session.create` with `source="gui"`, titled `Task <id>: <title>`), then submits the text
  (`prompt.submit`). Returns at once: task id, target profile, session id, title and a
  `hermes://open/<session id>` link.
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
  plugin state until that conversation takes them. A refused one is retried, in order, at the start
  of the next `profile_task` call or at the next event of its task; the gateway refuses a
  conversation that no client has open.
- Gateway restart: when the plugin loads, it finds the tasks whose waiting process is gone, marks
  them (`wait_lost`, or `failed` if the target never accepted the task), closes any approval that
  was open (the command did not run), and queues one notice per task to its manager conversation.
  It sends them once the gateway accepts plugin messages, then as above. When several processes load
  the plugin at once (a gateway launcher that then attaches to the running gateway, and that gateway),
  each task is closed and noticed by one of them only: the plugin state is read and written under a
  lock file in the plugin's data folder.
- Wait limit: 30 minutes by default. Past it the manager gets a message: no answer yet, the work
  is not cancelled, check back with `status`.
- Approvals in a task session: the plugin's connection to the target session sees its
  `approval.request` at once and injects a notice into the manager conversation (command, task,
  session, `hermes://open/<session>` link, expiry time from the target profile's
  `approvals.timeout`). When the request ends, a second notice gives the real outcome, read from the
  gated tool's result (its approval summary): approved and run, denied, expired, withdrawn, or
  ended with the turn. `approval` (action) shows the approve/deny prompt in the manager
  conversation itself; the choice made there (`once`, `session`, `always`, `deny`) is sent to the
  target session with `approval.respond`, so the command runs there. `always` permanently allows
  that kind of command in the target profile (its `command_allowlist`) and leaves one inert rule in
  the manager profile's config (`plugin_rule:profile_task_relay:<prompt id>`, one per relayed
  prompt, never matched again); the prompt's own text says so. Limits: the prompt opens only
  when the manager turn calls `approval` (a plugin cannot open an approval prompt in a conversation
  with no running turn); its card title is the generic plugin rule, the target command is in its
  description; it uses the manager profile's approval timeout, and if the manager's approvals are
  bypassed (yolo or mode off) nothing is passed on and the target request stays pending. If the
  target request ends (expired, answered in the target session, turn interrupted) while the card is
  open, the card is withdrawn and the manager is told the real ending; an answer that reaches the
  plugin after that ending is not recorded as pending nor sent to the target session.
  After the wait limit the plugin keeps watching the target turn (up to 24 h) only to relay approvals.
- MCP servers needing a new sign-in: at the start and at the end of every turn of a task, the plugin
  reads the target session's MCP server states (`session.info`, `mcp_servers`). A server whose
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
  the error text says 401 / unauthorized / invalid token. A stdio server's error text is not
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

## Enable (not done by this repo)

1. Link or copy `profile-tasks/` into the default profile's plugin folder:
   `ln -s ~/git/perso/hermes-profile-tasks/profile-tasks ~/.hermes/plugins/profile-tasks`
2. In `~/.hermes/config.yaml`:

   ```yaml
   plugins:
     enabled:
       - profile-tasks          # add to the existing list
     entries:
       profile-tasks:
         allow_gateway_injection: true   # lets the plugin inject the answer into the manager session
         settings:
           wait_timeout_seconds: 1800    # optional, default 1800
   ```

3. The gateway must serve the target profiles (`gateway.multiplex_profiles: true`, already the case
   when the desktop shows several profiles).
4. Restart the gateway.

Nothing is needed in the target profiles.

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
- `e2e/run.sh [trial ...]`: the end-to-end trials (`1-2-3 4 5 6 7 8 9 limit targets restart followup
  retry mcp_auth approval`; `approval` runs last because its relayed `always` permanently allows recursive
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

- Flaky e2e trial `retry` (pre-existing, not fixed here): its closing check
  `manager_saw('is waiting for an approval')` races the manager's model requests. It failed 5 runs
  out of 5 standalone on 379e94b.
