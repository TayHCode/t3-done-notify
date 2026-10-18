#!/usr/bin/env python3
"""Watch T3 Code threads, Claude Code sessions, and Codex sessions."""

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
SOURCE_NAMES = ("t3", "claude", "codex")
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
    sources: tuple
    claude_projects_dir: str
    codex_sessions_dir: str
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


def _sources(environment):
    raw = environment.get("T3_DONE_SOURCES")
    if raw is None or not str(raw).strip():
        return ("t3",)
    names = []
    for part in re.split(r"[\s,]+", str(raw).strip()):
        name = part.strip().lower()
        if not name:
            continue
        if name not in SOURCE_NAMES:
            raise ValueError("invalid T3_DONE_SOURCES")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("invalid T3_DONE_SOURCES")
    return tuple(names)


def _configured_path(environment, name):
    value = environment.get(name)
    if value is None or not str(value).strip():
        return ""
    return _expand_path(str(value).strip())


def _claude_projects_dir(environment):
    """Claude Code session root: ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects."""
    explicit = _configured_path(environment, "CLAUDE_PROJECTS_DIR")
    if explicit:
        return explicit
    config_dir = _configured_path(environment, "CLAUDE_CONFIG_DIR")
    if config_dir:
        return os.path.join(config_dir, "projects")
    return _expand_path("~/.claude/projects")


def _codex_sessions_dir(environment):
    """Codex rollout root: ~/.codex/sessions, or $CODEX_HOME/sessions."""
    explicit = _configured_path(environment, "CODEX_SESSIONS_DIR")
    if explicit:
        return explicit
    home = _configured_path(environment, "CODEX_HOME")
    if home:
        return os.path.join(home, "sessions")
    return _expand_path("~/.codex/sessions")


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
        sources=_sources(environment),
        claude_projects_dir=_claude_projects_dir(environment),
        codex_sessions_dir=_codex_sessions_dir(environment),
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
        "source": thread.get("source") or "t3",
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
        f"- Source: {payload.get('source', 't3')}\n"
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


def _commit_observation(state, record, thread_id, payload, now, save_path, daybook_dir, watched_id=None):
    """Persist one observation. A failed daybook write leaves the previous state in place."""
    if payload:
        append_daybook(payload, daybook_dir)
    candidate = dict(state)
    if watched_id is not None:
        candidate.pop(watched_id, None)
    candidate[thread_id] = record
    if payload and needs_attention(payload):
        candidate["_outbox"] = list(state.get("_outbox") or [])
        candidate["_outbox"].append({"payload": payload, "queued_at": now})
    save_state(candidate, save_path)
    state.clear()
    state.update(candidate)


def _jsonl_records(path):
    records = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def _block_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
            parts.append(str(part.get("text") or ""))
    return "".join(parts)


def _one_line(value, limit=120):
    lines = str(value or "").strip().splitlines()
    if not lines or not lines[0].strip():
        return ""
    return lines[0].strip()[:limit]


def _plain_text(value):
    text = str(value or "").strip()
    if not text or text.startswith("gAAAAA"):
        return ""
    return text


def _error_text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return message.strip()
    return ""


def _mtime_stamp(path):
    stamp = os.stat(path).st_mtime
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _updated_key(thread):
    parsed = parse_time(thread.get("updatedAt"))
    if parsed is None:
        return (0, thread.get("updatedAt") or "")
    return (1, parsed)


def _newer_session(left, right):
    if _updated_key(right) >= _updated_key(left):
        return right
    return left


def _session_thread(session_id, title, phase, updated, last_message, source):
    status = {
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "interrupted": "interrupted",
    }.get(phase, "idle")
    return {
        "id": session_id,
        "title": title or session_id,
        "status": status,
        "updatedAt": updated,
        "activeRunId": "open" if phase == "running" else None,
        "source": source,
        "last_message": last_message,
    }


def _claude_message_text(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    return _block_text(message.get("content")).strip()


def _claude_human_prompt(record):
    """A person-typed prompt. Tool results are user records too, and are not prompts."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, list):
        kinds = [part.get("type") for part in content if isinstance(part, dict)]
        if kinds and set(kinds) <= {"tool_result"}:
            return False
    return bool(_claude_message_text(record))


def _claude_has_tool_use(record):
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "tool_use"
        for part in message["content"]
    )


def _claude_api_failure(record):
    """Terminal API failure, including the synthetic assistant row Claude Code writes."""
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    if record.get("isApiErrorMessage") or message.get("isApiErrorMessage"):
        return True
    model = message.get("model")
    return (
        isinstance(model, str)
        and model.strip() == ""
        and message.get("stop_reason") == "stop_sequence"
    )


def parse_claude_session(path):
    """Read one Claude Code transcript.

    Session files are ``<projects>/<cwd-slug>/<sessionId>.jsonl``. A turn stays
    open until a ``turn_duration`` record or a terminal assistant stop reason
    (``end_turn``, ``stop_sequence``, ``max_tokens``) that is not waiting on a
    tool. ``isApiErrorMessage`` and an unrecovered ``api_error`` are failures.
    """
    records = _jsonl_records(path)
    if not records:
        return None
    session_id = ""
    custom_title = ""
    ai_title = ""
    first_prompt = ""
    updated = ""
    last_message = ""
    phase = "idle"
    pending_error = False
    terminal_stops = {"end_turn", "stop_sequence", "max_tokens"}

    for record in records:
        if record.get("sessionId"):
            session_id = str(record["sessionId"])
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            updated = timestamp
        kind = record.get("type")
        if kind == "custom-title" and record.get("customTitle"):
            custom_title = str(record["customTitle"])
        elif kind == "ai-title" and record.get("aiTitle"):
            ai_title = str(record["aiTitle"])
        elif kind == "summary" and record.get("summary") and not ai_title and not custom_title:
            ai_title = str(record["summary"])
        elif _claude_human_prompt(record):
            phase = "running"
            pending_error = False
            if not first_prompt:
                first_prompt = _one_line(_claude_message_text(record))
        elif kind == "assistant":
            text = _plain_text(_claude_message_text(record))
            if text:
                last_message = text
            if _claude_api_failure(record):
                phase = "failed"
                pending_error = False
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            stop = message.get("stop_reason")
            if stop in {"tool_use", "pause_turn"} or _claude_has_tool_use(record) or stop is None:
                phase = "running"
            elif stop in {"refusal", "model_context_window_exceeded"}:
                phase = "failed"
                pending_error = False
            elif stop in terminal_stops:
                phase = "completed"
                pending_error = False
        elif kind == "system":
            subtype = record.get("subtype")
            if subtype == "turn_duration":
                phase = "completed"
                pending_error = False
            elif subtype == "api_error" and str(record.get("level", "")).lower() == "error":
                pending_error = True
                phase = "running"
                text = _error_text(record.get("error"))
                if text:
                    last_message = text
            elif subtype == "stop_hook_summary" and record.get("preventedContinuation"):
                phase = "running"

    if pending_error and phase == "running":
        phase = "failed"
    if not session_id:
        session_id = os.path.splitext(os.path.basename(path))[0]
    if not updated:
        updated = _mtime_stamp(path)
    title = custom_title or ai_title or first_prompt
    return _session_thread(session_id, title, phase, updated, last_message, "claude")


def _codex_id_from_name(name):
    stem = name[:-6] if name.endswith(".jsonl") else name
    if not stem.startswith("rollout-"):
        return stem
    body = stem[len("rollout-"):]
    if len(body) > 20 and body[19] == "-":
        return body[20:]
    return body


def _codex_title_from_user(text):
    for line in str(text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("<") and not line.startswith("gAAAAA"):
            return line[:120]
    return ""


def _codex_error_message(error):
    text = _error_text(error)
    if text:
        return text
    if isinstance(error, dict):
        info = error.get("codex_error_info")
        if isinstance(info, str) and info.strip():
            return info.strip().replace("_", " ")
    return ""


def parse_codex_session(path):
    """Read one Codex rollout.

    Files live at ``<sessions>/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl``.
    Each line is ``{timestamp, type, payload}``. ``event_msg`` values
    ``task_started`` / ``turn_started`` open a turn; ``task_complete`` /
    ``turn_complete`` close it; ``turn_aborted`` closes an interrupt.
    ``task_complete.error`` and ``turn_aborted`` reason ``budget_limited`` are
    failures. The thread id is ``session_meta.id`` (a child agent's own id,
    not its parent ``session_id``).
    """
    records = _jsonl_records(path)
    if not records:
        return None
    session_id = ""
    title = ""
    updated = ""
    last_message = ""
    phase = "idle"

    for record in records:
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            updated = timestamp
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if kind == "session_meta" and not session_id:
            session_id = str(payload.get("id") or payload.get("session_id") or "")
        elif kind == "event_msg":
            event_name = str(payload.get("type") or "")
            if event_name in {"task_started", "turn_started"}:
                phase = "running"
            elif event_name in {"task_complete", "turn_complete"}:
                agent_message = payload.get("last_agent_message")
                if isinstance(agent_message, str):
                    text = _plain_text(agent_message)
                    if text:
                        last_message = text
                error = payload.get("error")
                if error:
                    phase = "failed"
                    error_message = _codex_error_message(error)
                    if error_message:
                        last_message = error_message
                else:
                    phase = "completed"
            elif event_name == "turn_aborted":
                if payload.get("reason") == "budget_limited":
                    phase = "failed"
                    if not last_message:
                        last_message = "budget limited"
                else:
                    phase = "interrupted"
            elif event_name == "user_message" and not title:
                title = _codex_title_from_user(payload.get("message") or "")
            elif event_name == "agent_message":
                text = _plain_text(payload.get("message") or "")
                if text and payload.get("phase") in {None, "final_answer"}:
                    last_message = text
        elif kind == "response_item":
            item = str(payload.get("type") or "")
            if item == "message" and payload.get("role") == "user" and not title:
                title = _codex_title_from_user(_block_text(payload.get("content")))
            elif item == "message" and payload.get("role") == "assistant":
                text = _plain_text(_block_text(payload.get("content")))
                if text:
                    last_message = text
            elif item == "agent_message":
                content = payload.get("content")
                text = _plain_text(content if isinstance(content, str) else _block_text(content))
                if text:
                    last_message = text

    if not session_id:
        session_id = _codex_id_from_name(os.path.basename(path))
    if not updated:
        updated = _mtime_stamp(path)
    return _session_thread(session_id, title, phase, updated, last_message, "codex")


def _read_session(path, parser):
    try:
        return parser(path)
    except Exception as exc:
        print(
            f"session read failed ({os.path.basename(path)}, {type(exc).__name__})",
            flush=True,
        )
        return None


def _remember_session(found, parsed):
    if not parsed or not parsed.get("id"):
        return
    current = found.get(parsed["id"])
    found[parsed["id"]] = parsed if current is None else _newer_session(current, parsed)


def claude_session_threads(root):
    """Top-level project transcripts only. Nested JSONL files are subagents, not sessions."""
    found = {}
    if not root or not os.path.isdir(root):
        return []
    try:
        projects = list(os.scandir(root))
    except OSError:
        print("session read failed", flush=True)
        return []
    for project in projects:
        try:
            if not project.is_dir(follow_symlinks=False):
                continue
            entries = list(os.scandir(project.path))
        except OSError:
            print("session read failed", flush=True)
            continue
        for entry in entries:
            name = entry.name
            if not name.endswith(".jsonl") or name.startswith("agent-"):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            _remember_session(found, _read_session(entry.path, parse_claude_session))
    return list(found.values())


def codex_session_threads(root):
    """Dated rollout files. Compressed ``.jsonl.zst`` archives are left unread."""
    found = {}
    if not root or not os.path.isdir(root):
        return []

    def walk_error(_error):
        print("session read failed", flush=True)

    for dirpath, _dirnames, filenames in os.walk(root, onerror=walk_error):
        for name in filenames:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(dirpath, name)
            _remember_session(found, _read_session(path, parse_codex_session))
    return list(found.values())


def poll_local_sessions(
    state,
    now=None,
    env=None,
    sessions=None,
    save_path=None,
    daybook_dir=None,
    config=None,
    dry_run=False,
):
    """Apply Claude Code and Codex transcripts through the T3 transition rules."""
    config = load_config() if config is None else config
    now = time.time() if now is None else now
    environment = config.environment if env is None else env
    validate_webhook(environment)
    save_path = config.state_file if save_path is None else save_path
    daybook_dir = config.daybook_dir if daybook_dir is None else daybook_dir
    if sessions is None:
        sessions = []
        if "claude" in config.sources:
            sessions.extend(claude_session_threads(config.claude_projects_dir))
        if "codex" in config.sources:
            sessions.extend(codex_session_threads(config.codex_sessions_dir))
    try:
        for thread in sessions:
            thread_id = thread["id"]
            previous_record = state.get(thread_id)
            # A session first seen already stopped is not a new completion.
            if previous_record is None and not active(thread):
                continue
            record = copy.deepcopy(previous_record) if isinstance(previous_record, dict) else {}
            payload = transition(
                record,
                thread,
                now,
                debounce_seconds=config.debounce_seconds,
                stall_seconds=config.stall_seconds,
            )
            if payload:
                payload["last_message"] = thread.get("last_message") or ""
            _commit_observation(
                state, record, thread_id, payload, now, save_path, daybook_dir
            )
    except Exception:
        flush_outbox(state, environment, now, save_path, dry_run=dry_run)
        raise
    return flush_outbox(state, environment, now, save_path, dry_run=dry_run)


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
            _commit_observation(
                state,
                record,
                thread_id,
                payload,
                now,
                save_path,
                daybook_dir,
                watched_id,
            )
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
    watch_local = "claude" in config.sources or "codex" in config.sources
    while True:
        poll_failed = False
        if "t3" in config.sources:
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
            except Exception as exc:
                print(f"poll failed ({type(exc).__name__})", flush=True)
                poll_failed = True
        if watch_local:
            try:
                state = poll_local_sessions(
                    state,
                    env=config.environment,
                    save_path=config.state_file,
                    daybook_dir=config.daybook_dir,
                    config=config,
                    dry_run=arguments.dry_run,
                )
            except Exception as exc:
                print(f"poll failed ({type(exc).__name__})", flush=True)
                poll_failed = True
        if not poll_failed:
            print("poll complete", flush=True)
        if arguments.once:
            return 1 if poll_failed else 0
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
