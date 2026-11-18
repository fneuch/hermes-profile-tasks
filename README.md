# profile-tasks (v0.1)

Hermes plugin for the manager (default profile). Its `profile_task` tool hands a task to another
profile served by the same Hermes gateway.

- `start`: creates one session under the target profile, the way the desktop does
  (`session.create` with `source="gui"`, titled `Task <id>: <title>`), then submits the text
  (`prompt.submit`). Returns at once: task id, target profile, session id, title and a
  `hermes://open/<session id>` link.
- When the target's turn ends, its final text is injected once into the calling manager
  conversation (`ctx.inject_message`), which starts a manager turn.
- `follow_up`: sends a new text to the same session (same context).
- `correct`: steers the running turn of the task (`prompt.receipt` then `session.steer`).
- `status`: lists the tasks from the plugin state and the allowed targets.
- Wait limit: 30 minutes by default. Past it the manager gets a message: no answer yet, the work
  is not cancelled, check back with `status`.
- Targets: every profile the gateway serves (`gateway_state.json` `served_profiles`) except the caller.
- The target runs with its own config, MCP servers and tokens. The manager only gets text back.
- At load time the plugin checks the Hermes internals it uses. If an update removed one, it logs
  an error naming it and the tool refuses every call with that message.

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
- `e2e/run.sh [trial ...]`: the end-to-end trials (`1-2-3 4 5 6 7 8 9 limit targets`). Evidence
  is written to `e2e/evidence/run-<timestamp>.json`. The analyst profile gets a fixture MCP server
  named `atlassian` (from the Hermes test fixtures) whose answer carries a per-run token set only
  in the analyst config.

Trial 9 (clicking the link in the desktop) is not covered: the isolated run has no desktop app.
