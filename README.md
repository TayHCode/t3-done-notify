# t3-done-notify

A small Python watcher for T3 Code threads, Claude Code sessions, and Codex
sessions. It keeps a local Markdown daybook and can POST a JSON webhook when a
watched session finishes, stalls, or errors and looks like it needs a person.

## Setup

From this directory:

```sh
./install.sh
```

The installer asks for two values, `WEBHOOK_URL` and `WEBHOOK_KEY`, then asks
which of T3 Code, Claude Code, and Codex you run. Any combination is fine,
including all three. It asks only for what the agents you picked need: T3 Code
still needs `T3_URL` and a read-only token. Claude Code and Codex use the
session files those tools already write. When `CLAUDE_PROJECTS_DIR` or
`CODEX_SESSIONS_DIR` is set, the installer saves that path in the env file
and the user service. A later run keeps the saved path if the variable is
unset.

It saves the env file in `${XDG_CONFIG_HOME:-~/.config}/t3-done` (mode 600)
and starts a background user service: systemd on Linux, launchd on macOS. Run `./install.sh` again to change
any of those answers. A re-run replaces the env file values, refreshes the
program, and restarts the service. State, the daybook, and an existing T3
watch list are kept.

T3 Code still watches only the thread IDs in `~/.config/t3-done/watch.txt`.
The installer copies an example there when that file is missing.

For a scripted run, pass the same answers in the environment:

```sh
WEBHOOK_URL=https://hooks.example.invalid/notify \
WEBHOOK_KEY=replace-me \
T3_DONE_SOURCES=claude,codex \
  ./install.sh --non-interactive
```

Add `T3_URL` and either `T3_TOKEN` (the token text) or `T3_TOKEN_FILE` when
`T3_DONE_SOURCES` includes `t3`. `T3_TOKEN` is an installer input; the watcher
reads the token from the token file, not from the env file.

## What it does

- Polls every 60 seconds.
- T3 Code: reads a read-only shell snapshot and tracks IDs in `watch.txt`.
  Full IDs are preferred; unambiguous prefixes of the last colon-separated ID
  segment also work.
- Claude Code: reads session transcripts at
  `~/.claude/projects/<project>/<session-id>.jsonl`. Nested files, including
  `subagents/` and `agent-*.jsonl`, are not sessions and are ignored.
- Codex: reads rollout files at
  `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl`.
  A resumed thread can span more than one rollout; those files are one session.
  Cold `.jsonl.zst` archives are skipped.
- Records a completion after a previously active session stays stopped for 120
  seconds. Resuming during that interval cancels the pending completion.
- Records a stall when an active session has not updated for 45 minutes, once
  per unchanged activity timestamp. Claude Code and Codex use this same stall
  rule.
- Appends the event and full last message to a daily Markdown file. A failed
  session, or a last message containing a question or an attention phrase such
  as "blocked", "approval", or "could you", also queues a webhook. Ordinary
  successful completions stay in the daybook. The attention check is a text
  heuristic; it can miss requests and flag questions that do not require action.
- Persists queued notifications before delivery. Only an HTTP 2xx response
  acknowledges delivery. Failed notifications remain queued across restarts,
  without an age, count, or message-length cutoff.
- Sends one POST for one session end. A later poll of the same completion does
  not queue another. Codex rollouts that share a thread id are one session.

Claude Code and Codex remember each session file by its path, modification
time, and size. Files already finished when that index is first written are
not new completions. A file that appears or grows between polls is recorded
even when the session has already stopped, and it still waits out the debounce
before one POST. For T3, add the thread to the watch list before its next run.
A "done" event means stopped, not necessarily successful: waiting, idle,
cancelled, and interrupted statuses also qualify. An open turn that is already
older than the stall window is reported once when the watcher first sees it.

## Requirements and compatibility

Python 3.10 or newer and the dependency in `requirements.txt`. T3 Code needs a
reachable server and an existing `orchestration:read` token. Claude Code and
Codex need their local session directories. Linux with systemd, or macOS with
launchd, is used by the installer to run the watcher in the background.

The bundled `t3ctl` implements just the read calls used here: the websocket ticket
endpoint, protocol 2 `orchestration.subscribeShell`, and
`orchestration.getThreadProjection`. These are T3 implementation interfaces;
upstream changes may require an adapter update. The tests exercise synthetic
snapshots and protocol frames, not a specific released T3 version. This tool
never dispatches a message, starts a run, or issues or renews a token.

Claude Code and Codex transcript shapes are internal and can change between
releases. The watcher reads the fields those tools write today: Claude Code
`turn_duration`, assistant `stop_reason`, and `isApiErrorMessage` / `api_error`;
Codex `event_msg` values `task_started`, `task_complete`, and `turn_aborted`
(plus the `turn_started` / `turn_complete` aliases).

## Configuration

Configuration is read at process startup. Settings in the process environment
override settings in `<config_dir>/env`. The file is simple `KEY=VALUE` data:
blank lines and comments are ignored, optional surrounding quotes are accepted,
and shell substitutions are not evaluated. Restart your own instance after
changing configuration. The T3 watch list is reread each poll.

- `T3_DONE_CONFIG_DIR`: chooses the configuration directory before the env file is
  read. Set it in the process environment. Default:
  `${XDG_CONFIG_HOME:-~/.config}/t3-done`. The installer writes this into the
  user service so the watcher reads the file it just wrote.
- `T3_DONE_ENV_FILE`: optional env-file path, also selected from the process
  environment before the file is read. Default `<config_dir>/env`.
- `T3_DONE_SOURCES`: comma-separated list of `t3`, `claude`, and `codex`.
  Default `t3`, so an existing T3-only install keeps that behavior.
- `T3_DONE_WATCH_FILE`: default `<config_dir>/watch.txt`. Used for T3 only.
- `T3_DONE_STATE_FILE`: default
  `${XDG_STATE_HOME:-~/.local/state}/t3-done/state.json`.
- `T3_DONE_DAYBOOK_DIR`: default
  `${XDG_STATE_HOME:-~/.local/state}/t3-done/daybook`.
- `T3_DONE_POLL_SECONDS`: default `60`.
- `T3_DONE_DEBOUNCE_SECONDS`: default `120`.
- `T3_DONE_STALL_SECONDS`: default `2700`.
- `T3_DONE_T3CTL`: optional executable path for a compatible read-only client.
  By default, the adjacent bundled client runs with the notifier's Python
  interpreter, so it uses the same virtual environment.
- `T3_DONE_T3CTL_TIMEOUT_SECONDS`: default `30`, the total timeout for each client
  subprocess. Increase this for a slow server or large snapshots. The bundled
  client also applies finite HTTP, WebSocket, and RPC timeouts.
- `T3_URL`: default `http://localhost:3773`. For a remote server use HTTPS.
  Read when `t3` is one of the sources.
- `T3_TOKEN_FILE`: default `<config_dir>/t3-token`; contains an existing T3
  read-only bearer token, not the webhook key.
- `CLAUDE_PROJECTS_DIR`: Claude Code transcript root. Default
  `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects` when
  `CLAUDE_CONFIG_DIR` is set. The installer saves this in the env file and the
  user service when it is set, and keeps the saved path on a later run when
  the variable is unset.
- `CODEX_SESSIONS_DIR`: Codex rollout root. Default `~/.codex/sessions`, or
  `$CODEX_HOME/sessions` when `CODEX_HOME` is set. The installer saves and
  keeps it the same way as `CLAUDE_PROJECTS_DIR`.
- `WEBHOOK_URL` and `WEBHOOK_KEY`: unset by default. `KEY` is accepted as a legacy
  alias for `WEBHOOK_KEY`; prefer the explicit name for new setups.

Poll, stall, and client timeout values must be positive finite numbers. Debounce
may be zero.
Use absolute paths for overrides to avoid dependence on a service's working
directory. The example `env` file passes the T3 connection settings to the
bundled client as well as configuring the notifier.

## Background service

`./install.sh` installs and starts the service. On Linux it copies
`t3-done-notify.service` to `~/.config/systemd/user/`, writes a drop-in that
sets `T3_DONE_CONFIG_DIR` and any saved `CLAUDE_PROJECTS_DIR` or
`CODEX_SESSIONS_DIR`, and runs `systemctl --user enable` and `restart`.
On macOS it writes `~/Library/LaunchAgents/com.t3-done-notify.plist` with
those variables and loads it with `launchctl`. Re-running the installer
restarts the same service rather than starting a second copy.

The program reads its env file itself; the unit does not source it. User-manager
environments may differ from your interactive shell. This unit does not change
login lingering or system services.

Use one running notifier per state file. Do not point a test instance at a live
instance's state, watch list, daybook, credentials, or webhook receiver. To move
from an older installation, stop **your own** old instance before transferring its
state, or explicitly keep its old path with `T3_DONE_STATE_FILE`. The program does
not discover or migrate old state files automatically.

Logs:

```sh
journalctl --user -u t3-done-notify.service -f
```

On macOS the installer sends stdout and stderr to
`~/.local/state/t3-done/notify.log`.

## Manual install

The installer is the supported setup. To place the files yourself:

```sh
install -d "$HOME/.local/share/t3-done-notify"
install -m 644 t3_done_notify.py t3ctl requirements.txt \
  "$HOME/.local/share/t3-done-notify/"
python3 -m venv "$HOME/.local/share/t3-done-notify/.venv"
"$HOME/.local/share/t3-done-notify/.venv/bin/python" -m pip install \
  -r "$HOME/.local/share/t3-done-notify/requirements.txt"
```

Copy `env.example` to `~/.config/t3-done/env` (mode 600) and set `WEBHOOK_URL`,
`WEBHOOK_KEY`, and `T3_DONE_SOURCES`. For T3, set `T3_URL`, put a read-only
token in `~/.config/t3-done/t3-token`, and list thread IDs in `watch.txt`.
Blank lines and lines beginning with `#` are ignored in the watch list.

Run once without making webhook requests:

```sh
"$HOME/.local/share/t3-done-notify/.venv/bin/python" \
  "$HOME/.local/share/t3-done-notify/t3_done_notify.py" --once --dry-run
```

This still reads the enabled sources and updates local state/daybook; it does not
consume pending notifications. Run without `--once` for continuous polling and
without `--dry-run` to enable configured webhook delivery.

With `--once`, configuration or polling errors return a nonzero exit status.
A successful poll can still leave webhook deliveries pending; delivery failures
stay queued and are reported separately in the logs.

## Webhook contract and delivery

The receiver gets `POST` JSON with `Authorization: Bearer <WEBHOOK_KEY>` and
`Content-Type: application/json` and a stable `Idempotency-Key` header. For example (all values are synthetic):

```json
{
  "thread_id": "thread:project:example:example-thread",
  "title": "Example task",
  "status": "completed",
  "reason": "done",
  "updated_at": "2026-01-01T12:00:00Z",
  "last_message": "Could you approve the result?",
  "source": "t3",
  "event_id": "005ade536113ef9eb1c1d96e03adeff2f58142869b9c3b4d1ee1c02080d336d0"
}
```

`source` is `t3`, `claude`, or `codex`. `reason` is `done`, `error`, or
`stalled`. `thread_id` is the T3 thread id, the Claude Code session id, or the
Codex thread id (`session_meta.id`). The entire last message is included; it is
not summarized or truncated. Failed attempts use short retries, then the queue
is tried again on later polls. Redirects are rejected, including redirects to
an apparently equivalent URL: configure the final receiver URL directly.
Response bodies are not logged.

Delivery can be duplicated if the receiver accepts a request but the response is
lost or the process exits before persisting its acknowledgment. Disk or process failures can also cause a repeated daybook entry. Delivery is at-least-once: exactly-once delivery cannot be guaranteed
over HTTP. Every JSON payload includes a stable `event_id`, also sent as the
`Idempotency-Key` HTTP header. Receivers should durably dedupe on this key before
applying side effects; retries after timeouts or acknowledgement crashes carry
the same key. Existing reason, status, updated_at, last_message and source
fields are unchanged. Legacy queued events receive a deterministic identity
based on their stored completion key or timestamp.

Local completion identities are stored persistently without a cap, independent
of transcript paths. Renaming or rotating unchanged content does not notify
again. State storage grows with the number of distinct completed turns.

## Security and operations

The daybook, state, watch list, and webhook payload can contain private task
content, thread IDs, and titles. Claude Code and Codex transcripts are read from
the local account and the same text can be posted. Protect local directories and
choose the receiver accordingly. HTTP sends bearer credentials without transport
encryption; reserve it for trusted local loopback tests and use HTTPS for remote
destinations. Both T3 authentication requests and webhook requests reject
redirects. Keep config and token files at mode `600`; new state and daybook
files are created privately.

Treat Markdown messages as untrusted content when viewing them. Logs report
operational status rather than printing webhook keys, bearer tokens, response
bodies, or full notification payloads. Do not share config/state/daybook files or
private scan reports with a public repository.

The queue and daybook have no automatic retention limit. Monitor disk space and
rotate/archive data according to your own policy. Removing an ID from the watch
list prevents new T3 events, but already queued events are still pending delivery.
Changing a webhook receiver also changes where existing queued events will go.
Review the queue with the notifier stopped before intentionally discarding it.

## Tests

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -q
```

Tests use temporary directories, mock T3 protocol traffic, sample Claude Code
and Codex session files, and loopback-only HTTP servers. They do not need a
running T3 instance, real tokens, or a real webhook.

## Limitations

- Delivery is at least once, not exactly once: a crash between a 2xx response and saving state can resend a notice.
- "Needs attention" is a text heuristic over the last message; it can miss or over-report.
- The queue and daybook grow without limit; rotate or prune them yourself.
- Tested against the T3 protocol-2 interfaces only; other T3 versions are untested.
- Claude Code and Codex file formats are internal. A future release can move a field the watcher reads.
- Codex `.jsonl.zst` archives are not read.
- Tested on Python 3.14; Python 3.10 syntax is checked but its runtime is not.

## License

MIT. See [LICENSE](LICENSE).
