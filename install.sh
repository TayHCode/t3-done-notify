#!/bin/sh
# Install t3-done-notify and start it as a user service.
#
# Interactive:
#   ./install.sh
# Asks for WEBHOOK_URL and WEBHOOK_KEY, which of T3 Code, Claude Code, and
# Codex to watch, and (only for T3) T3_URL plus a read-only token.
#
# Non-interactive (same answers as environment variables):
#   ./install.sh --non-interactive [--no-service]
# Requires WEBHOOK_URL, WEBHOOK_KEY, and T3_DONE_SOURCES.
# When sources include t3, also requires T3_URL and either T3_TOKEN_FILE
# or T3_TOKEN (token text; written to the token file, not the env file).
# CLAUDE_PROJECTS_DIR and CODEX_SESSIONS_DIR, when set, are saved into the
# env file and the user service. A later run keeps a saved path when the
# variable is unset.
#
# Re-running updates the env file, refreshes the program, and restarts the
# service. Existing state, daybook, and watch.txt are left in place.
set -eu

usage() {
  printf '%s\n' "Usage: ./install.sh [--non-interactive] [--no-service]"
}

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

restore_echo() {
  stty echo 2>/dev/null || true
}

one_line() {
  printf '%s' "$1" | awk 'NR>1 { found=1 } END { exit found ? 1 : 0 }'
}

env_value() {
  file=$1
  key=$2
  [ -f "$file" ] || return 0
  awk -F= -v key="$key" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    {
      k = $1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      if (k == key) {
        v = substr($0, index($0, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
        gsub(/^["'\'']|["'\'']$/, "", v)
        print v
      }
    }
  ' "$file" | tail -n 1
}

set_env_key() {
  file=$1
  key=$2
  value=$3
  one_line "$value" || die "$key must be a single line"
  tmp="$file.tmp.$$"
  found=0
  if [ -f "$file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "$key"=*)
          printf '%s=%s\n' "$key" "$value" >> "$tmp"
          found=1
          ;;
        *)
          printf '%s\n' "$line" >> "$tmp"
          ;;
      esac
    done < "$file"
  fi
  if [ "$found" -eq 0 ]; then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

unset_env_key() {
  file=$1
  key=$2
  [ -f "$file" ] || return 0
  tmp="$file.tmp.$$"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key"=*) ;;
      *) printf '%s\n' "$line" >> "$tmp" ;;
    esac
  done < "$file"
  if [ ! -f "$tmp" ]; then
    : > "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

normalize_sources() {
  raw=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  raw=$(printf '%s' "$raw" | sed 's/t3 code/t3/g; s/claude code/claude/g; s/codex cli/codex/g')
  raw=$(printf '%s' "$raw" | tr ',/' '  ')
  out=
  for token in $raw; do
    case $token in
      t3|t3code|t3-code) canon=t3 ;;
      claude|claude-code|claudecode) canon=claude ;;
      codex) canon=codex ;;
      *) return 1 ;;
    esac
    case ",$out," in
      *",$canon,"*) ;;
      *)
        if [ -n "$out" ]; then
          out="$out,$canon"
        else
          out=$canon
        fi
        ;;
    esac
  done
  [ -n "$out" ] || return 1
  printf '%s' "$out"
}

has_source() {
  case ",$sources," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

valid_url() {
  case "$1" in
    http://*|https://*) ;;
    *) return 1 ;;
  esac
  rest=${1#*://}
  authority=${rest%%/*}
  [ -n "$authority" ] || return 1
  case "$authority" in
    *@*) return 1 ;;
  esac
  return 0
}

prompt() {
  question=$1
  default=${2-}
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$question" "$default" >&2
  else
    printf '%s: ' "$question" >&2
  fi
  answer=
  IFS= read -r answer || true
  if [ -z "$answer" ]; then
    printf '%s' "$default"
  else
    printf '%s' "$answer"
  fi
}

prompt_secret() {
  printf '%s: ' "$1" >&2
  stty -echo 2>/dev/null || true
  secret=
  IFS= read -r secret || true
  stty echo 2>/dev/null || true
  printf '\n' >&2
  printf '%s' "$secret"
}

absolute_path() {
  target=$1
  if [ ! -e "$target" ]; then
    die "path does not exist"
  fi
  directory=$(CDPATH= cd -- "$(dirname "$target")" && pwd)
  printf '%s/%s' "$directory" "$(basename "$target")"
}

normalize_dir() {
  raw=$1
  one_line "$raw" >/dev/null || return 1
  case "$raw" in
    *\"*|*\'*) return 1 ;;
  esac
  case "$raw" in
    "~") raw=$HOME ;;
    "~/"*) raw="$HOME/${raw#"~/"}" ;;
  esac
  case "$raw" in
    /*) printf '%s' "$raw" ;;
    *) printf '%s/%s' "$(pwd)" "$raw" ;;
  esac
}

resolve_saved_dir() {
  key=$1
  current=$2
  if [ -n "$current" ]; then
    normalize_dir "$current" || return 1
  else
    env_value "$env_file" "$key" || true
  fi
}

xml_escape() {
  printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

systemd_escape_percent() {
  printf '%s' "$1" | sed 's/%/%%/g'
}

write_launchd_plist() {
  dest=$1
  python=$2
  script=$3
  config=$4
  log_dir=$5
  claude_dir=${6-}
  codex_dir=${7-}
  mkdir -p "$(dirname "$dest")" "$log_dir"
  session_env_xml=
  if [ -n "$claude_dir" ]; then
    session_env_xml="${session_env_xml}    <key>CLAUDE_PROJECTS_DIR</key>
    <string>$(xml_escape "$claude_dir")</string>
"
  fi
  if [ -n "$codex_dir" ]; then
    session_env_xml="${session_env_xml}    <key>CODEX_SESSIONS_DIR</key>
    <string>$(xml_escape "$codex_dir")</string>
"
  fi
  cat > "$dest" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.t3-done-notify</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$python")</string>
    <string>$(xml_escape "$script")</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$(xml_escape "$HOME")</string>
    <key>T3_DONE_CONFIG_DIR</key>
    <string>$(xml_escape "$config")</string>
$session_env_xml  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$log_dir/notify.log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$log_dir/notify.log")</string>
</dict>
</plist>
EOF
  chmod 644 "$dest"
}

noninteractive=0
no_service=0
for arg in "$@"; do
  case "$arg" in
    --non-interactive) noninteractive=1 ;;
    --no-service) no_service=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

trap restore_echo EXIT
umask 077

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  die "Python 3.10 or newer is required"
fi

root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/t3-done"
mkdir -p "$config_dir"
chmod 700 "$config_dir"
config_dir=$(CDPATH= cd -- "$config_dir" && pwd)
env_file="$config_dir/env"
token_path="$config_dir/t3-token"
share="$HOME/.local/share/t3-done-notify"
python="$share/.venv/bin/python"
script="$share/t3_done_notify.py"

if [ "$noninteractive" -eq 1 ]; then
  webhook_url=${WEBHOOK_URL:-}
  webhook_key=${WEBHOOK_KEY:-}
  [ -n "$webhook_url" ] || die "WEBHOOK_URL is required"
  [ -n "$webhook_key" ] || die "WEBHOOK_KEY is required"
  if ! sources=$(normalize_sources "${T3_DONE_SOURCES:-}"); then
    die "T3_DONE_SOURCES must list any of: t3, claude, codex"
  fi
else
  webhook_url=$(env_value "$env_file" WEBHOOK_URL || true)
  while :; do
    webhook_url=$(prompt "Webhook URL" "$webhook_url")
    if valid_url "$webhook_url"; then
      break
    fi
    printf '%s\n' "Enter an http or https URL without user info." >&2
  done
  existing_key=$(env_value "$env_file" WEBHOOK_KEY || true)
  while :; do
    if [ -n "$existing_key" ]; then
      entered=$(prompt_secret "Webhook key (empty keeps the saved key)")
      if [ -n "$entered" ]; then
        webhook_key=$entered
      else
        webhook_key=$existing_key
      fi
      break
    fi
    webhook_key=$(prompt_secret "Webhook key")
    if [ -n "$webhook_key" ]; then
      break
    fi
    printf '%s\n' "A webhook key is required." >&2
  done
  sources=$(env_value "$env_file" T3_DONE_SOURCES || true)
  while :; do
    raw=$(prompt "Agents to watch (t3, claude, codex)" "$sources")
    if normalized=$(normalize_sources "$raw"); then
      sources=$normalized
      break
    fi
    printf '%s\n' "Pick any of: t3, claude, codex. All three are allowed." >&2
  done
fi

valid_url "$webhook_url" || die "WEBHOOK_URL must be an http or https URL without user info"
one_line "$webhook_url" || die "WEBHOOK_URL must be a single line"
one_line "$webhook_key" || die "WEBHOOK_KEY must be a single line"
[ -n "$webhook_key" ] || die "WEBHOOK_KEY is required"

if ! claude_projects_dir=$(resolve_saved_dir CLAUDE_PROJECTS_DIR "${CLAUDE_PROJECTS_DIR:-}"); then
  die "CLAUDE_PROJECTS_DIR must be a single path without quotes or newlines"
fi
if ! codex_sessions_dir=$(resolve_saved_dir CODEX_SESSIONS_DIR "${CODEX_SESSIONS_DIR:-}"); then
  die "CODEX_SESSIONS_DIR must be a single path without quotes or newlines"
fi

t3_url=
token_file_setting=
token_to_write=
if has_source t3; then
  if [ "$noninteractive" -eq 1 ]; then
    t3_url=${T3_URL:-}
    [ -n "$t3_url" ] || die "T3_URL is required when t3 is selected"
    if [ -n "${T3_TOKEN_FILE:-}" ]; then
      [ -s "$T3_TOKEN_FILE" ] || die "T3_TOKEN_FILE is missing or empty"
      token_file_setting=$(absolute_path "$T3_TOKEN_FILE")
    elif [ -n "${T3_TOKEN:-}" ]; then
      one_line "$T3_TOKEN" || die "T3_TOKEN must be a single line"
      token_to_write=$T3_TOKEN
    elif [ ! -s "$token_path" ]; then
      die "T3_TOKEN or T3_TOKEN_FILE is required when t3 is selected"
    fi
  else
    t3_url=$(env_value "$env_file" T3_URL || true)
    if [ -z "$t3_url" ]; then
      t3_url="http://localhost:3773"
    fi
    while :; do
      t3_url=$(prompt "T3 URL" "$t3_url")
      if valid_url "$t3_url"; then
        break
      fi
      printf '%s\n' "Enter an http or https T3 URL without user info." >&2
    done
    existing_token_file=$(env_value "$env_file" T3_TOKEN_FILE || true)
    token_choice=$(prompt "Existing T3 token file (empty to paste a token)" "$existing_token_file")
    if [ -n "$token_choice" ]; then
      [ -s "$token_choice" ] || die "T3 token file is missing or empty"
      token_file_setting=$(absolute_path "$token_choice")
    elif [ -s "$token_path" ]; then
      token_to_write=$(prompt_secret "T3 read-only token (empty keeps the saved token)")
    else
      while :; do
        token_to_write=$(prompt_secret "T3 read-only token")
        if [ -n "$token_to_write" ]; then
          break
        fi
        printf '%s\n' "A T3 read-only token is required." >&2
      done
    fi
  fi
  valid_url "$t3_url" || die "T3_URL must be an http or https URL without user info"
fi

mkdir -p "$share"
install -m 644 "$root/t3_done_notify.py" "$root/t3ctl" "$root/requirements.txt" "$share/"
if [ ! -x "$python" ]; then
  if ! python3 -m venv "$share/.venv"; then
    die "Could not create a virtualenv. On Debian/Ubuntu, install python3-venv and run ./install.sh again."
  fi
fi
"$python" -m pip install -q -r "$share/requirements.txt"

if [ ! -f "$env_file" ]; then
  printf '%s\n' "# Written by install.sh. KEY=VALUE data, not a shell script." > "$env_file"
  chmod 600 "$env_file"
fi
set_env_key "$env_file" WEBHOOK_URL "$webhook_url"
set_env_key "$env_file" WEBHOOK_KEY "$webhook_key"
set_env_key "$env_file" T3_DONE_SOURCES "$sources"
if has_source t3; then
  set_env_key "$env_file" T3_URL "$t3_url"
  if [ -n "$token_to_write" ]; then
    one_line "$token_to_write" || die "T3 token must be a single line"
    printf '%s\n' "$token_to_write" > "$token_path"
    chmod 600 "$token_path"
  fi
  if [ -n "$token_file_setting" ]; then
    set_env_key "$env_file" T3_TOKEN_FILE "$token_file_setting"
  else
    unset_env_key "$env_file" T3_TOKEN_FILE
  fi
  if [ ! -f "$config_dir/watch.txt" ]; then
    install -m 600 "$root/watch.example" "$config_dir/watch.txt"
  fi
else
  unset_env_key "$env_file" T3_URL
  unset_env_key "$env_file" T3_TOKEN_FILE
fi
if [ -n "$claude_projects_dir" ]; then
  set_env_key "$env_file" CLAUDE_PROJECTS_DIR "$claude_projects_dir"
fi
if [ -n "$codex_sessions_dir" ]; then
  set_env_key "$env_file" CODEX_SESSIONS_DIR "$codex_sessions_dir"
fi
chmod 600 "$env_file"

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir/t3-done-notify.service.d"
install -m 644 "$root/t3-done-notify.service" "$unit_dir/t3-done-notify.service"
systemd_config_dir=$(systemd_escape_percent "$config_dir")
systemd_claude_projects_dir=$(systemd_escape_percent "$claude_projects_dir")
systemd_codex_sessions_dir=$(systemd_escape_percent "$codex_sessions_dir")
{
  printf '%s\n' "[Service]"
  printf 'Environment="%s=%s"\n' T3_DONE_CONFIG_DIR "$systemd_config_dir"
  if [ -n "$claude_projects_dir" ]; then
    printf 'Environment="%s=%s"\n' CLAUDE_PROJECTS_DIR "$systemd_claude_projects_dir"
  fi
  if [ -n "$codex_sessions_dir" ]; then
    printf 'Environment="%s=%s"\n' CODEX_SESSIONS_DIR "$systemd_codex_sessions_dir"
  fi
} > "$unit_dir/t3-done-notify.service.d/config.conf"
chmod 644 "$unit_dir/t3-done-notify.service.d/config.conf"

log_dir="${XDG_STATE_HOME:-$HOME/.local/state}/t3-done"
mkdir -p "$log_dir"
plist="$HOME/Library/LaunchAgents/com.t3-done-notify.plist"
os_name=$(uname -s)
if [ "$os_name" = "Darwin" ]; then
  write_launchd_plist "$plist" "$python" "$script" "$config_dir" "$log_dir" \
    "$claude_projects_dir" "$codex_sessions_dir"
fi

if [ "$no_service" -eq 0 ]; then
  case "$os_name" in
    Linux)
      command -v systemctl >/dev/null 2>&1 || die "systemctl is required to start the user service"
      systemctl --user daemon-reload
      systemctl --user enable t3-done-notify.service
      systemctl --user restart t3-done-notify.service
      printf '%s\n' "Started systemd user service t3-done-notify.service."
      ;;
    Darwin)
      command -v launchctl >/dev/null 2>&1 || die "launchctl is required to start the user service"
      domain="gui/$(id -u)"
      launchctl bootout "$domain/com.t3-done-notify" 2>/dev/null || true
      if ! launchctl bootstrap "$domain" "$plist"; then
        launchctl load -w "$plist"
      fi
      launchctl enable "$domain/com.t3-done-notify" 2>/dev/null || true
      printf '%s\n' "Started launchd agent com.t3-done-notify."
      ;;
    *)
      die "No user-service install for $os_name. Re-run with --no-service and start the watcher yourself."
      ;;
  esac
else
  printf '%s\n' "Service start skipped."
fi

printf '%s\n' "Config: $env_file"
printf '%s\n' "Sources: $sources"
if has_source t3; then
  printf '%s\n' "T3 thread IDs: $config_dir/watch.txt"
fi
