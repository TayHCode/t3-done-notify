# t3-done-notify

A small Python service that watches selected T3 Code threads, keeps a local
Markdown daybook, and sends a JSON webhook when a stopped thread appears to need
attention. This directory is self-contained: copy it or extract it as its own Git
repository. It does not require the surrounding operator tools.

## What it does

- Polls a read-only T3 shell snapshot every 60 seconds.
- Tracks only IDs in `watch.txt`. Full IDs are preferred; unambiguous prefixes of
  the last colon-separated ID segment also work.
- Records a completion after a previously active thread stays stopped for 120
  seconds. Resuming during that interval cancels the pending completion.
- Records a stall when an active thread has not updated for 45 minutes, once per
  unchanged activity timestamp.
- Appends the event and full last message to a daily Markdown file. A failed
  thread, or a last message containing a question or an attention phrase such as
  “blocked”, “approval”, or “could you”, also queues a webhook. Ordinary successful
  completions stay in the daybook. The attention check is a text heuristic; it
  can miss requests and flag questions that do not require action.
- Persists queued notifications before delivery. Only an HTTP 2xx response
  acknowledges delivery. Failed notifications remain queued across restarts,
  without an age, count, or message-length cutoff.

A thread first observed already stopped is not a new completion. Add it to the
watch list before its next run. A “done” event means stopped, not necessarily
successful: waiting, idle, cancelled, and interrupted statuses also qualify.

## Requirements and compatibility

Python 3.10 or newer, the dependency in `requirements.txt`, and a reachable T3
Code server with an existing `orchestration:read` token. Linux with systemd is
needed only for the optional user-service example.

The bundled `t3ctl` implements just the read calls used here: the websocket ticket
endpoint, protocol 2 `orchestration.subscribeShell`, and
`orchestration.getThreadProjection`. These are T3 implementation interfaces;
upstream changes may require an adapter update. The tests exercise synthetic
snapshots and protocol frames, not a specific released T3 version. This tool
never dispatches a message, starts a run, or issues or renews a token.

## Install

Run these commands from this directory (or the root of its extracted repository).
They install into a fixed per-user location used by the unit example:

```sh
install -d "$HOME/.local/share/t3-done-notify"
install -m 644 t3_done_notify.py t3ctl requirements.txt \
  "$HOME/.local/share/t3-done-notify/"
python3 -m venv "$HOME/.local/share/t3-done-notify/.venv"
"$HOME/.local/share/t3-done-notify/.venv/bin/python" -m pip install \
  -r "$HOME/.local/share/t3-done-notify/requirements.txt"

config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/t3-done"
install -d -m 700 "$config_dir"
# Copy examples only when no configuration already exists.
test -e "$config_dir/env" || install -m 600 env.example "$config_dir/env"
test -e "$config_dir/watch.txt" || install -m 600 watch.example "$config_dir/watch.txt"
```

Edit `watch.txt` to contain your own thread IDs, one per line. Blank lines and
lines beginning with `#` are ignored. Obtain a token with only
`orchestration:read` through your T3 installation's supported authentication
workflow, save it privately to `<config_dir>/t3-token` with mode `600`, and keep
its containing directory private. You can instead set `T3_TOKEN_FILE` to an
existing read-only token file. Expired or missing tokens require operator action;
the notifier will not create or renew credentials for you.

For webhook delivery, edit `<config_dir>/env` and set **both** `WEBHOOK_URL` and
`WEBHOOK_KEY`. Use an HTTPS receiver you control that accepts the payload below.
Never put real credentials in the source tree. A partially configured pair is an
error. With neither value configured, events are recorded locally and attention
events remain queued for later delivery. Adding credentials later can therefore
send the accumulated backlog.

Run once without making webhook requests:

```sh
"$HOME/.local/share/t3-done-notify/.venv/bin/python" \
  "$HOME/.local/share/t3-done-notify/t3_done_notify.py" --once --dry-run
```

This still reads T3 and updates local state/daybook; it does not consume pending
notifications. An empty watch list is a useful first configuration. Run without
`--once` for continuous polling and without `--dry-run` to enable configured
webhook delivery.

With `--once`, configuration or polling errors return a nonzero exit status.
A successful poll can still leave webhook deliveries pending; delivery failures
stay queued and are reported separately in the logs.

## Configuration

Configuration is read at process startup. Settings in the process environment
override settings in `<config_dir>/env`. The file is simple `KEY=VALUE` data:
blank lines and comments are ignored, optional surrounding quotes are accepted,
and shell substitutions are not evaluated. Restart your own instance after
changing configuration. The watch list is reread each poll.

- `T3_DONE_CONFIG_DIR`: chooses the configuration directory before the env file is
  read. Set it in the process environment. Default:
  `${XDG_CONFIG_HOME:-~/.config}/t3-done`.
- `T3_DONE_ENV_FILE`: optional env-file path, also selected from the process
  environment before the file is read. Default `<config_dir>/env`.
- `T3_DONE_WATCH_FILE`: default `<config_dir>/watch.txt`.
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
- `T3_TOKEN_FILE`: default `<config_dir>/t3-token`; contains an existing T3
  read-only bearer token, not the webhook key.
- `WEBHOOK_URL` and `WEBHOOK_KEY`: unset by default. `KEY` is accepted as a legacy
  alias for `WEBHOOK_KEY`; prefer the explicit name for new setups.

Poll, stall, and client timeout values must be positive finite numbers. Debounce
may be zero.
Use absolute paths for overrides to avoid dependence on a service's working
directory. The example `env` file passes the T3 connection settings to the
bundled client as well as configuring the notifier.

## systemd --user example

The included `t3-done-notify.service` uses the installation path above, sets a
private umask, and restarts after process failures. These are installation steps
for your own instance; review the paths and existing service before replacing it:

```sh
install -D -m 644 t3-done-notify.service \
  "$HOME/.config/systemd/user/t3-done-notify.service"
systemctl --user daemon-reload
systemctl --user enable --now t3-done-notify.service
journalctl --user -u t3-done-notify.service -f
```

The program reads its env file itself; the unit does not source it. For a custom
config location, add `Environment=T3_DONE_CONFIG_DIR=/absolute/private/path` in a
unit override. User-manager environments may differ from your interactive shell;
set required XDG variables in the unit override too. This unit does not change
login lingering or system services.

Use one running notifier per state file. Do not point a test instance at a live
instance's state, watch list, daybook, credentials, or webhook receiver. To move
from an older installation, stop **your own** old instance before transferring its
state, or explicitly keep its old path with `T3_DONE_STATE_FILE`. The program does
not discover or migrate old state files automatically.

## Webhook contract and delivery

The receiver gets `POST` JSON with `Authorization: Bearer <WEBHOOK_KEY>` and
`Content-Type: application/json`. For example (all values are synthetic):

```json
{
  "thread_id": "thread:project:example:example-thread",
  "title": "Example task",
  "status": "completed",
  "reason": "done",
  "updated_at": "2026-01-01T12:00:00Z",
  "last_message": "Could you approve the result?"
}
```

`reason` is `done`, `error`, or `stalled`. The entire last message is included;
it is not summarized or truncated. Failed attempts use short retries, then the
queue is tried again on later polls. Redirects are rejected, including redirects
to an apparently equivalent URL: configure the final receiver URL directly.
Response bodies are not logged.

Delivery can be duplicated if the receiver accepts a request but the response is
lost or the process exits before persisting its acknowledgment. Receivers should
handle duplicates, for example using `thread_id`, `reason`, and `updated_at` as a
key. Disk or process failures can also cause a repeated daybook entry. This is a
small polling tool, not an exactly-once messaging system.

## Security and operations

The daybook, state, watch list, and webhook payload can contain private task
content, thread IDs, and titles. Protect local directories and choose the receiver
accordingly. HTTP sends bearer credentials without transport encryption; reserve
it for trusted local loopback tests and use HTTPS for remote destinations. Both
T3 authentication requests and webhook requests reject redirects. Keep config
and token files at mode `600`; new state and daybook files are created privately.

Treat Markdown messages as untrusted content when viewing them. Logs report
operational status rather than printing webhook keys, bearer tokens, response
bodies, or full notification payloads. Do not share config/state/daybook files or
private scan reports with a public repository.

The queue and daybook have no automatic retention limit. Monitor disk space and
rotate/archive data according to your own policy. Removing an ID from the watch
list prevents new events, but already queued events are still pending delivery.
Changing a webhook receiver also changes where existing queued events will go.
Review the queue with the notifier stopped before intentionally discarding it.

## Tests

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -q
```

Tests use temporary directories, mock T3 protocol traffic, and loopback-only HTTP
servers. They do not need a running T3 instance, real tokens, or a real webhook.

## Limitations

- Delivery is at least once, not exactly once: a crash between a 2xx response and saving state can resend a notice.
- "Needs attention" is a text heuristic over the last message; it can miss or over-report.
- The queue and daybook grow without limit; rotate or prune them yourself.
- Tested against the T3 protocol-2 interfaces only; other T3 versions are untested.
- Tested on Python 3.14; Python 3.10 syntax is checked but its runtime is not.

## License

MIT. See [LICENSE](LICENSE).
