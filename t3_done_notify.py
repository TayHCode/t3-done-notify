#!/usr/bin/env python3
"""Poll t3ctl for watched threads and notify a configured webhook."""

import argparse
import copy
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

POLL_SECONDS = 60.0
DEBOUNCE_SECONDS = 120.0
STALL_SECONDS = 45 * 60.0
T3CTL_TIMEOUT_SECONDS = 30.0
ACTIVE_STATUSES = {"running", "working", "starting", "preparing", "queued"}
STOPPED_STATUSES = {
    "completed", "failed", "cancelled", "canceled", "interrupted", "idle", "waiting"
}
RETRY_DELAYS = (1, 2, 4, 8)
ATTENTION_PATTERN = re.compile(
    r"\b(blocked|approval|approve|permission|need(?:s)? (?:you|someone)|"
    r"please (?:confirm|approve)|yes/no|(?:can|could|would) you)\b|\?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Config:
    config_dir: str
    env_file: str
    watch_file: str
    state_file: str
    daybook_dir: str
    t3ctl: str
    use_python_t3ctl: bool
    poll_seconds: float
    debounce_seconds: float
    stall_seconds: float
    t3ctl_timeout_seconds: float
    environment: dict


def _expand_path(value):
    return os.path.expanduser(value)


def _number(environment, name, default, *, allow_zero=False):
    value = environment.get(name, str(default))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name}") from None
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"invalid {name}")
    return number


def read_env(path):
    """Read simple KEY=VALUE settings without echoing values in errors."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip("\"'")
    return values


def validate_webhook(environment):
    url = environment.get("WEBHOOK_URL", "").strip()
    key = (environment.get("WEBHOOK_KEY") or environment.get("KEY") or "").strip()
    if bool(url) != bool(key):
        raise ValueError("WEBHOOK_URL and WEBHOOK_KEY must be configured together")
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WEBHOOK_URL must be an HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("WEBHOOK_URL must not contain user information")
    return url, key


def load_config(environ=None):
    """Resolve paths and settings; process environment overrides the env file."""
    process_environment = dict(os.environ if environ is None else environ)
    config_dir = process_environment.get("T3_DONE_CONFIG_DIR")
    if not config_dir:
        xdg_config = process_environment.get("XDG_CONFIG_HOME")
        config_dir = os.path.join(xdg_config, "t3-done") if xdg_config else "~/.config/t3-done"
    config_dir = _expand_path(config_dir)
    env_file = _expand_path(process_environment.get(
        "T3_DONE_ENV_FILE", os.path.join(config_dir, "env")
    ))
    environment = read_env(env_file)
    environment.update(process_environment)
    environment.setdefault("T3_TOKEN_FILE", os.path.join(config_dir, "t3-token"))
    validate_webhook(environment)

    xdg_state = environment.get("XDG_STATE_HOME")
    state_dir = os.path.join(xdg_state, "t3-done") if xdg_state else "~/.local/state/t3-done"
    bundled_t3ctl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t3ctl")
    t3ctl_override = environment.get("T3_DONE_T3CTL")
    t3ctl = _expand_path(t3ctl_override) if t3ctl_override else bundled_t3ctl
    return Config(
        config_dir=config_dir,
        env_file=env_file,
        watch_file=_expand_path(environment.get(
            "T3_DONE_WATCH_FILE", os.path.join(config_dir, "watch.txt")
        )),
        state_file=_expand_path(environment.get(
            "T3_DONE_STATE_FILE", os.path.join(state_dir, "state.json")
        )),
        daybook_dir=_expand_path(environment.get(
            "T3_DONE_DAYBOOK_DIR", os.path.join(state_dir, "daybook")
        )),
        t3ctl=t3ctl,
        use_python_t3ctl=not bool(t3ctl_override),
        poll_seconds=_number(environment, "T3_DONE_POLL_SECONDS", POLL_SECONDS),
        debounce_seconds=_number(
            environment, "T3_DONE_DEBOUNCE_SECONDS", DEBOUNCE_SECONDS, allow_zero=True
        ),
        stall_seconds=_number(environment, "T3_DONE_STALL_SECONDS", STALL_SECONDS),
        t3ctl_timeout_seconds=_number(
            environment, "T3_DONE_T3CTL_TIMEOUT_SECONDS", T3CTL_TIMEOUT_SECONDS
        ),
        environment=environment,
    )


def parse_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def active(thread):
    return bool(thread.get("activeRunId")) or str(thread.get("status", "")).lower() in ACTIVE_STATUSES


def stopped(thread):
    return not thread.get("activeRunId") and str(thread.get("status", "")).lower() in STOPPED_STATUSES


def transition(state, thread, now, last_message="", debounce_seconds=None, stall_seconds=None):
    """Update one thread record and return a ready event, if one is due."""
    debounce_seconds = DEBOUNCE_SECONDS if debounce_seconds is None else debounce_seconds
    stall_seconds = STALL_SECONDS if stall_seconds is None else stall_seconds
    status = str(thread.get("status", "unknown")).lower()
    updated = thread.get("updatedAt") or ""
    state.setdefault("active", False)
    state.setdefault("pending_since", None)
    state.setdefault("pending_status", None)
    state.setdefault("stalled_updated_at", None)

    if active(thread):
        state["active"] = True
        state["pending_since"] = None
        state["pending_status"] = None
        activity = parse_time(updated)
        if activity is not None and now - activity >= stall_seconds:
            if state.get("stalled_updated_at") != updated:
                state["stalled_updated_at"] = updated
                return event(thread, "stalled", last_message)
        else:
            state["stalled_updated_at"] = None
        return None

    state["stalled_updated_at"] = None
    if stopped(thread) and state.get("active"):
        if state.get("pending_since") is None:
            state["pending_since"] = now
            state["pending_status"] = status
            return None
        if now - state["pending_since"] >= debounce_seconds:
            state["active"] = False
            state["pending_since"] = None
            reason = "error" if status == "failed" else "done"
            return event(thread, reason, last_message)
    else:
        state["active"] = False
        state["pending_since"] = None
        state["pending_status"] = None
    return None


def event(thread, reason, last_message):
    return {
        "thread_id": thread["id"],
        "title": thread.get("title", ""),
        "status": thread.get("status", "unknown"),
        "reason": reason,
        "updated_at": thread.get("updatedAt", ""),
        "last_message": last_message,
    }


def needs_attention(payload):
    if str(payload.get("status", "")).lower() == "failed":
        return True
    return bool(ATTENTION_PATTERN.search(str(payload.get("last_message", ""))))


def append_daybook(payload, directory=None):
    if directory is None:
        directory = load_config().daybook_dir
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, dt.datetime.now().strftime("%Y-%m-%d") + ".md")
    fence = chr(96) * 4
    content = (
        f"## {payload.get('title', '')}\n\n"
        f"- ID: {payload['thread_id']}\n"
        f"- Status: {payload.get('status', 'unknown')}\n\n"
        f"Final message:\n\n{fence}text\n"
        f"{payload.get('last_message', '')}"
    )
    if payload.get("last_message") and not payload["last_message"].endswith("\n"):
        content += "\n"
    content += f"{fence}\n\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_state(path=None):
    if path is None:
        path = load_config().state_file
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def save_state(state, path=None):
    if path is None:
        path = load_config().state_file
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".t3-done-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def watch_ids(path=None):
    if path is None:
        path = load_config().watch_file
    try:
        with open(path, encoding="utf-8") as handle:
            values = [
                line.strip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except FileNotFoundError:
        return []
    return list(dict.fromkeys(values))


def _t3ctl_command(config, arguments):
    if config.use_python_t3ctl:
        return [sys.executable, config.t3ctl, *arguments]
    return [config.t3ctl, *arguments]


def _run_t3ctl(arguments, *, env=None, config=None):
    config = load_config() if config is None else config
    environment = config.environment if env is None else env
    return subprocess.check_output(
        _t3ctl_command(config, arguments),
        text=True,
        stderr=subprocess.DEVNULL,
        env=environment,
        timeout=config.t3ctl_timeout_seconds,
    )


def snapshot(env=None, config=None):
    raw = _run_t3ctl(["lanes", "--json", "--include-archived"], env=env, config=config)
    return {row["id"]: row for row in json.loads(raw)}


def last_message(thread_id, env=None, config=None):
    raw = _run_t3ctl(["thread", thread_id, "--json"], env=env, config=config)
    project = json.loads(raw)
    messages = project.get("messages") or []
    if not messages:
        return ""
    message = messages[-1]
    value = message.get("text")
    if value is None and isinstance(message.get("content"), list):
        value = "".join(
            part.get("text", "")
            for part in message["content"]
            if isinstance(part, dict)
        )
    return value or ""


def _webhook_opener():
    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            return None

    return urllib.request.build_opener(NoRedirectHandler)


def deliver(payload, env):
    """POST with short retries; return True only for a 2xx response."""
    credentials = validate_webhook(env)
    if credentials is None:
        return False
    url, key = credentials
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    opener = _webhook_opener()
    tries = len(RETRY_DELAYS) + 1
    for attempt in range(tries):
        try:
            with opener.open(request, timeout=20) as response:
                if 200 <= response.status < 300:
                    return True
                print(
                    f"webhook attempt {attempt + 1}/{tries} failed (HTTP {response.status})",
                    flush=True,
                )
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            print(
                f"webhook attempt {attempt + 1}/{tries} failed (HTTP {status})",
                flush=True,
            )
        except Exception as exc:
            print(
                f"webhook attempt {attempt + 1}/{tries} failed ({type(exc).__name__})",
                flush=True,
            )
        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
    return False


def flush_outbox(state, env, now=None, save_path=None, dry_run=False):
    """Deliver pending events; failures remain queued until a later poll."""
    del now  # Kept in the signature for compatibility.
    outbox = state.get("_outbox") or []
    if not outbox:
        return state
    credentials = validate_webhook(env)
    if dry_run:
        for item in outbox:
            payload = item["payload"]
            print(
                f"dry-run: pending notification for {payload['thread_id']} "
                f"({payload['reason']})",
                flush=True,
            )
        return state
    if credentials is None:
        return state

    keep = []
    candidate = dict(state)
    changed = False
    for item in outbox:
        payload = item["payload"]
        if deliver(payload, env):
            record = candidate.get(payload["thread_id"])
            if isinstance(record, dict):
                updated_record = dict(record)
                updated_record["last_fired"] = (
                    payload["reason"] + ":" + str(payload["updated_at"])
                )
                candidate[payload["thread_id"]] = updated_record
            changed = True
        else:
            keep.append(item)
    if changed:
        candidate["_outbox"] = keep
        save_state(candidate, save_path)
        state.clear()
        state.update(candidate)
    return state


def _matches(watched_id, snap):
    if watched_id in snap:
        return snap[watched_id]
    matches = [
        row for actual_id, row in snap.items()
        if actual_id.rsplit(":", 1)[-1].startswith(watched_id)
    ]
    if len(matches) > 1:
        print(f"watch id {watched_id} is ambiguous", flush=True)
        return None
    return matches[0] if matches else None


def poll_once(
    state,
    now=None,
    env=None,
    snap=None,
    message_reader=None,
    save_path=None,
    watch_path=None,
    daybook_dir=None,
    config=None,
    dry_run=False,
):
    config = load_config() if config is None else config
    now = time.time() if now is None else now
    environment = config.environment if env is None else env
    validate_webhook(environment)
    save_path = config.state_file if save_path is None else save_path
    watch_path = config.watch_file if watch_path is None else watch_path
    daybook_dir = config.daybook_dir if daybook_dir is None else daybook_dir
    if message_reader is None:
        message_reader = lambda thread_id: last_message(thread_id, environment, config)

    try:
        snap = snapshot(environment, config) if snap is None else snap
        for watched_id in watch_ids(watch_path):
            thread = _matches(watched_id, snap)
            if not thread:
                continue
            thread_id = thread["id"]
            previous_record = state.get(thread_id)
            record = copy.deepcopy(previous_record) if previous_record is not None else {}
            prefix_record = state.get(watched_id)
            if previous_record is None and prefix_record is not None:
                record = copy.deepcopy(prefix_record)
            payload = transition(
                record,
                thread,
                now,
                debounce_seconds=config.debounce_seconds,
                stall_seconds=config.stall_seconds,
            )
            if payload:
                payload["last_message"] = message_reader(thread_id)
                append_daybook(payload, daybook_dir)

            candidate = dict(state)
            candidate.pop(watched_id, None)
            candidate[thread_id] = record
            if payload and needs_attention(payload):
                candidate["_outbox"] = list(state.get("_outbox") or [])
                candidate["_outbox"].append({"payload": payload, "queued_at": now})
            save_state(candidate, save_path)
            state.clear()
            state.update(candidate)
    except Exception:
        flush_outbox(state, environment, now, save_path, dry_run=dry_run)
        raise

    return flush_outbox(state, environment, now, save_path, dry_run=dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show pending notifications without sending or acknowledging them",
    )
    arguments = parser.parse_args(argv)
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        print(f"configuration error ({type(exc).__name__})", flush=True)
        return 2

    os.makedirs(config.config_dir, mode=0o700, exist_ok=True)
    try:
        env_mode = os.stat(config.env_file).st_mode
    except FileNotFoundError:
        env_mode = 0
    if env_mode & 0o077:
        print("warning: notifier env file permissions should be 600", flush=True)
    if validate_webhook(config.environment) is None:
        print(
            "webhook is not configured; writing the daybook and retaining attention events locally",
            flush=True,
        )

    state = load_state(config.state_file)
    while True:
        poll_failed = False
        try:
            state = poll_once(
                state,
                env=config.environment,
                save_path=config.state_file,
                watch_path=config.watch_file,
                daybook_dir=config.daybook_dir,
                config=config,
                dry_run=arguments.dry_run,
            )
            print("poll complete", flush=True)
        except Exception as exc:
            print(f"poll failed ({type(exc).__name__})", flush=True)
            poll_failed = True
        if arguments.once:
            return 1 if poll_failed else 0
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
