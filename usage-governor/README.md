# usage-governor (v0.1)

Hermes plugin that sets the agents' usage mode from the Claude plan usage, with no model. Its rules and
values are the upstream governor's, as is. It decides and records the mode, shows it
in the desktop status bar and, through its own `pre_tool_call` hook, blocks the task starts and
delegation options the mode does not allow.

- Every minute a Hermes cron job with no agent, `usage-governor`, runs one tick: it reads the usage with
  `hermes usage --json` (window `Current session` = 5 hours, `Current week` = week; other windows are
  ignored), decides the mode, writes `state.json`, appends one line to `journal.jsonl` and touches
  `heartbeat`. A normal tick prints nothing, so the cron run stays silent.
- On working days (Monday to Friday), the first tick at or after `daily_review` (16:55) that is not in
  quiet mode prints one line, delivered by the cron job:
  `REVIEW daily usage · 5h {five}% · 7d {week}% · log {journal path}` (a % without a trailing `.0`, `--`
  without `%` when unknown).

## Install

From a checkout of this repository, one command:

```
usage-governor/install.sh
```

By default it installs and enables the plugin (`hermes -p <profile> plugins install
fneuch/hermes-profile-tasks/usage-governor --enable`) in the default profile and in every profile
found under `$HERMES_HOME/profiles/` (`~/.hermes/profiles/` when `HERMES_HOME` is unset; a profile's home
counts as its root). To limit it to some profiles, name them: `install.sh default <profile>...`
(`default` is the default profile). A profile that already has the plugin is only enabled, so running it
again is safe, e.g. after adding a profile. It always passes `-p`, so the sticky active profile
(`hermes profile use`) is never the one changed by mistake.

The usage tick runs from the default profile only: install it there at least. The mode hook (see
Enforcement) applies in every profile where the plugin is installed: those `install.sh` installs it in,
the default one and every profile found, or the ones named.

When the plugin loads under the default profile (gateway, desktop or CLI), it:

1. writes or refreshes `~/.hermes/scripts/usage-governor-tick.py`, a thin wrapper that runs this plugin's
   `tick.py` (Hermes cron runs scripts from `HERMES_HOME/scripts/` only); the wrapper exits 0, silently,
   when the plugin directory is gone;
2. creates the cron job when no job named `usage-governor` exists, paused ones included, through Hermes'
   cron API (the one `hermes cron create` calls): schedule `every 1m`, no agent, script
   `usage-governor-tick.py`, delivery `local`, failure delivery `local`.

Under any other profile it installs no job. Set `schedule: false` to stop it creating the job; remove
or pause the job with `hermes cron`. A job that exists is never changed: delete it to have the next load
create it again.

## Enforcement

Install the plugin in each profile you want controlled; the tick only runs in the default profile:

```
hermes -p <profile> plugins install fneuch/hermes-profile-tasks/usage-governor --enable
```

In every profile that loads it, the plugin registers a `pre_tool_call` hook. At each `profile_task` or
`delegate_task` call it reads the default profile's `state.json` again (no cache) and applies its `mode`
(an active `override.json` is already folded into it by the tick):

| Mode | `profile_task` start | Options |
| --- | --- | --- |
| `full` | allowed | any |
| `reduced` | allowed | `reduced_options` only |
| `essential`, `budget`, `quiet` | blocked | `reduced_options` only |

- Only `profile_task` with `action: start` counts as a start; `follow_up`, `correct`, `status` and
  `approval` are never blocked.
- `delegate_task` (spawn, the default action): every `tasks[].option` must be in `reduced_options`; one
  task with another option, or none, blocks the whole call. The action is matched case-insensitively and
  a `tasks` sent as a JSON string is parsed first (unparsable: no option). The legacy single-task form (top-level
  `goal`) names no option and is blocked. The control actions (`list`, `steer`, `stop`, `continue`) are
  not checked.
- `profile_task`: options are checked only when the call carries an `option` key; a call without one
  is not option-checked.
- A `tool_call` wrapper is checked as the tool it wraps (Hermes unwraps it before the hook; the hook
  also opens one that reaches it).
- Fail open: no `state.json`, an unreadable one, a wrong setting or any error in the hook blocks
  nothing (each logged once per process). A stale state (no tick for over 10 minutes) still applies its last mode.

A blocked call gets, as its tool result, one line naming what was refused, the mode and why, then the
expected return time, local (`HH:MM`, with the weekday when not today):

```
BLOCKED by usage-governor: delegate_task: option Deep is not allowed in reduced mode; allowed: Fast, Balance. Expected back at 12:30 (local).
```

The return time is the tick's `until_local` from `state.json` (see Files); the hook computes no time
itself and reads no timezone or quiet hours. A state written by an older tick (no `until_local`), or
one whose `until` is already past (the tick stopped), gives the message without a return time.

## Desktop

A status-bar item, on the right (left of profile-tasks), shows `<mode> · 5h <n>% · wk <n>%`, for example
`full · 5h 12% · wk 40%`; `--` stands for a value the state does not have. It reads `GET
/api/plugins/usage-governor/state` (`dashboard/plugin_api.py`, read-only: `state.json` and the health
check below) every 30 s, so an override shows within about 90 s (60 s tick + 30 s poll). When the health check fails or there is
no state, a warning icon shows next to the text.
The icon also shows when the last reading is missing (`reading: missing`): the week % is then the last good one;
and on a settings error (`settings_error`), when the tick no longer updates the %.
The tooltip gives the mode, each % with its reset time,
an active override and its end time, why the reading is missing, a settings error and the health reasons.
The item has no click action.

The desktop part and the route come with the install. The app copies the desktop part when it reloads:
after the install, restart the gateway and reload the app (Ctrl+R).

## Modes

Most restrictive first; the first rule that holds wins.

| Mode | When |
| --- | --- |
| `quiet` | local time inside `quiet_hours` (02:50 to 08:00; a range may wrap midnight) |
| `budget` | week % used today >= the day budget |
| `essential` | top >= `essential_at` (75), or used today >= 0.8 × day budget |
| `reduced` | top >= `reduced_at` (50), or used today >= 0.6 × day budget |
| `full` | otherwise |

- top = the larger of the 5-hour and week %, skipping a window whose reset time is past (or unknown;
  a reset time no date can hold, e.g. 1e20, counts as unknown).
- Day budget = max(`daily_percent_of_week`, round((100 − week %) / working days left before the week
  resets)), set at the day's first reading. The day starts at the minute the quiet hours end; before
  that minute it is the previous calendar date (the upstream rule: with quiet hours ending 08:00, 07:59 still
  counts as the day before; with no quiet hours, at midnight); its baseline is the week % at its first
  reading, taken again on a new day or when the week % drops (week reset).
- Missing reading: `hermes usage` exits non-zero, takes over 15 s, prints no JSON, sets
  `unavailable_reason`, lacks one of the two windows, or its `fetched_at` is over 900 s old. Then
  the tick keeps the last state's week % and drops the 5-hour window (sets it to null), the upstream rule, so
  the mode can loosen when the 5-hour window drove it. The same applies when both windows are past
  their reset. `state.json` then says `reading: missing`, `source_ok: false` and the `reason`, and keeps
  `reading_at` of the last usable reading.
- `read_usage()` is the contract's source shape (`(five_hour_pct, week_pct, resets)` or None);
  `usage_reading()` gives the same plus why the reading is missing, and the tick uses that one.

## Settings

`plugins.entries.usage-governor.settings` in `config.yaml`: the default profile's for the tick, read at
each tick; each profile's own for `enforce` and `reduced_options` (see below):

| Setting | Default | Meaning |
| --- | --- | --- |
| `timezone` | `America/Toronto` | zone of the quiet hours, the review and the working days |
| `quiet_hours` | `["02:50", "08:00"]` | start and end (HH:MM); `[]` = none |
| `daily_review` | `"16:55"` | time of the review line (HH:MM); `""` = none |
| `daily_percent_of_week` | `20` | smallest day budget, % of the week |
| `reduced_at` | `50` | top % from which the mode is reduced |
| `essential_at` | `75` | top % from which the mode is essential |
| `schedule` | `true` | create the cron job when the plugin loads |
| `enforce` | `true` | the hook blocks; `false` = it blocks nothing |
| `reduced_options` | `["Fast", "Balance"]` | delegation options allowed outside `full` mode |

Which profile's settings count:

- `enforce` and `reduced_options` are per profile: the hook reads them from the config of the profile it
  runs in, at each call. A wrong value turns that profile's hook off until fixed, logged once; it does
  not stop the tick.
- Every other setting (`timezone`, `quiet_hours`, the thresholds, ...) is the tick's, read from the
  default profile only. The hook never reads them: the times it quotes come from `state.json`.

A wrong value of the tick's settings stops the tick: it records the error in `state.json` (`settings_error`) and changes
nothing else (no mode change, no journal line, no heartbeat, so the health check fails after 10 min).

## Files

In `plugin-data/usage-governor/` of the default profile (`~/.hermes/plugin-data/usage-governor/`),
whatever profile runs the tick. Each JSON file is written to a temporary file, then renamed over it.

### `state.json`

| Field | Meaning |
| --- | --- |
| `mode` | `quiet`, `budget`, `essential`, `reduced` or `full` |
| `since` | epoch seconds the mode started |
| `five_hour`, `seven_day` | % used of the 5-hour window and the week; null when unknown |
| `resets` | `{"five_hour": "Wed 12:30", "seven_day": "Sun 05:00"}`, local reset times |
| `reading_at` | epoch seconds of the last usable reading; null until the first usable reading |
| `source_ok` | this tick had a usable reading (false when the fallback was taken) |
| `reading` | `ok` or `missing` |
| `reason` | why the reading is missing or unused, else null |
| `override` | `{"active": bool, "until": epoch or null, "ignored": why override.json was ignored, or null}`; `until` is also null when the override's `until` is out of range, while the override stays active |
| `quiet` | local time is inside `quiet_hours` |
| `until` | epoch seconds the mode is expected to end; null in `full` unless an override is active, and null when the override's `until` is out of range |
| `until_local` | `until` in the tick's `timezone`: `HH:MM` when today, else `Ddd HH:MM`; null when `until` is |
| `day` | `{"day": "YYYY-MM-DD", "start": week % baseline, "budget": day budget, "used": week % used today}` |
| `reviewed` | day (`YYYY-MM-DD`) of the last review line |
| `settings_error` | the wrong setting that stopped the tick, else null |

`until` is: the override's `until` when one is active, whatever its mode (`full` included); the end of
the quiet hours in `quiet`; the start of the next day (end of the quiet hours, one day later, weekend
included) in `budget`; in `essential` and `reduced`, the LATEST of the resets of the windows at or above
the mode's threshold (`essential_at` or `reduced_at`) and, when the day share holds the mode too (used
today >= 0.8 or 0.6 × day budget), the next day's start, because the mode drops only when all of them
have; with none of these, the next day's start.

### `journal.jsonl`

One line per tick: `{"at": epoch, "five_hour": %, "seven_day": %, "mode": "...", "source_ok": bool}`.

### `override.json`

Written by hand: `{"mode": "full", "until": 1795700000}` (epoch seconds). It wins over the rules while
now < `until` (no `mode` = `full`); expired or malformed, it is ignored and may be deleted. A `mode`
that is not one of `quiet`, `budget`, `essential`, `reduced`, `full` is ignored too (the rules decide)
and `state.json` `override.ignored` says why. Read every tick. An override replaces only the mode: the
day's usage is still counted while it is active (`day.json` and `state.json` `day` keep the day's
baseline, budget and use), so the budget and the carry-over see the spending and the rules apply it once
the override ends.

### `day.json`

The day's record: `{"day": "YYYY-MM-DD", "start": week % baseline, "budget": day budget}`.

### `heartbeat`

Touched by every tick that read its settings.

### `install.lock`

Lock file taken by the plugin load while it looks up and creates the cron job, so two processes
loading the plugin at once do not create two jobs. Empty; safe to delete.

## Health

Run the wrapper with the Hermes install's Python (the dependency venv cron runs scripts with; the system
`/usr/bin/python3` cannot import `hermes_cli`):

```
PY=$(python3 -c 'import json,glob,sys; f=sorted(glob.glob(sys.argv[1]+"/installs/*/facts.json"))[0]; print(json.load(open(f))["packages"]["venv"]["environment"]+"/bin/python")' ~/.hermes)
"$PY" ~/.hermes/scripts/usage-governor-tick.py --health
```

It prints one line and exits 0 when the heartbeat is younger than 10 minutes and `state.json` is
readable, 1 otherwise with the reasons.

## Hermes internals

`register()` checks these after the hook's own internals and, when one is gone after a Hermes update, logs the list and installs
no cron job: `hermes_cli.profiles.get_profile_dir` / `profile_matches_home`,
`hermes_cli.config.load_config_readonly`, `hermes_constants.get_hermes_home` and
`tools.cronjob_tools.cronjob` with the parameters it passes. The usage itself comes from the
`hermes usage --json` command, not from an import. Under another profile the check does not run. When
the profile check itself raises, `register()` logs the error with that list and installs nothing.

The hook's own internals, `PluginContext.register_hook` and `pre_tool_call` in
`hermes_cli.plugins.VALID_HOOKS`, are checked separately in every profile: a gap is logged and never
stops the cron job install. The hook is registered in any case (a failed registration is logged); it
fails open when an internal it uses is gone.

## Tests

Offline, from this directory, with the Hermes install's Python (`$PY`, see Health):

```
"$PY" -m unittest discover -s tests
```

A bare `python3` cannot import `hermes_cli`: the real-tree register tests are then skipped.

`tests/test_enforce.py` covers the hook; its real-tree cases (skipped without `hermes_cli`) also drive
Hermes' `tool_call` dispatch to show the hook sees a wrapped `profile_task` as `profile_task`.

`install.sh`, with a fake `hermes` on PATH in a throwaway root under `/tmp`:

```
bash tests/test_install.sh
```

`tests/fixtures/hermes-usage.json` is the output of a real `hermes usage --json`. `tests/test_status_bar.py`
needs the Hermes install's Python (FastAPI, `hermes_cli`) and `node` for the status-bar render
(`tests/status_render.mjs`, SDK and React stubbed).
