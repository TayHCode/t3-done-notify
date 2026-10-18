#!/usr/bin/env bash
# Source this file from formal/run.sh. It installs only pinned, checksum-verified
# tools under ~/.local/share/fv-tools/t3-done-notify and sets FV_JAVA/FV_TLA_JAR.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  printf 'Source this file from a Bash script to set FV_JAVA and FV_TLA_JAR.\n' >&2
  exit 2
fi

_fv_toolchain_init() {
  local system machine fv_user_home script_dir lockfile config
  local jdk_version jdk_archive jdk_url jdk_sha tla_version tla_artifact tla_url tla_sha
  local tool_root cache_root jdk_root archive_path jar_path stage candidate java_output

  system=$(uname -s) || return 1
  machine=$(uname -m) || return 1
  if [[ "$system" != Linux || "$machine" != x86_64 ]]; then
    printf 'formal toolchain supports Linux x86_64 only (found %s %s).\n' "$system" "$machine" >&2
    return 1
  fi
  fv_user_home=${HOME:-}
  if [[ -z "$fv_user_home" ]]; then
    printf 'HOME must be set to install the formal verification tools.\n' >&2
    return 1
  fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v sha256sum >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    printf 'formal toolchain requires curl, sha256sum, tar, and python3.\n' >&2
    return 1
  fi

  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1
  lockfile="$script_dir/toolchain.lock.json"
  if [[ ! -f "$lockfile" ]]; then
    printf 'toolchain lock file is missing: %s\n' "$lockfile" >&2
    return 1
  fi
  if ! config=$(python3 - "$lockfile" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    lock = json.load(stream)
if lock.get("schema_version") != 2:
    raise SystemExit("unsupported toolchain lock schema")
jdk = lock["jdk"]
tla = lock["tla"]
components = jdk["version_components"]
build = jdk["build"]
platform = jdk["platform"]
if components != [21, 0, 12, 1] or any(type(part) is not int for part in components):
    raise SystemExit("unexpected JDK release components in lock")
if type(build) is not int or build != 1 or platform != "x64_linux_hotspot":
    raise SystemExit("unexpected JDK build or platform in lock")
version = ".".join(map(str, components))
archive = f"OpenJDK{components[0]}U-jdk_{platform}_{version}_{build}.tar.gz"
base = jdk["release_base_url"]
if base != "https://github.com/adoptium/temurin21-binaries/releases/download":
    raise SystemExit("unexpected JDK source in lock")
url = f"{base}/jdk-{version}%2B{build}/{archive}"
values = [
    f"{version}+{build}", archive, url, jdk["sha256"],
    tla["version"], tla["artifact"], tla["url"], tla["sha256"],
]
if values[4] != "1.8.0" or values[5] != "tla2tools.jar":
    raise SystemExit("unexpected TLA+ artifact in lock")
if not values[6].startswith("https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/"):
    raise SystemExit("unexpected TLA+ source in lock")
for digest in (values[3], values[7]):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("invalid SHA-256 in lock")
for value in values:
    if "\t" in value or "\n" in value:
        raise SystemExit("invalid tab/newline in toolchain lock value")
print("\t".join(values))
PY
  ); then
    printf 'could not read or validate toolchain lock: %s\n' "$lockfile" >&2
    return 1
  fi
  IFS=$'\t' read -r jdk_version jdk_archive jdk_url jdk_sha tla_version tla_artifact tla_url tla_sha <<< "$config"

  tool_root="$fv_user_home/.local/share/fv-tools/t3-done-notify"
  cache_root="$tool_root/cache"
  jdk_root="$tool_root/jdk-$jdk_version"
  archive_path="$cache_root/$jdk_archive"
  jar_path="$tool_root/${tla_artifact%.jar}-$tla_version.jar"
  mkdir -p -- "$cache_root" || return 1

  _fv_toolchain_sha256_matches() {
    local expected actual
    expected=$1
    actual=$(sha256sum -- "$2") || return 1
    actual=${actual%% *}
    [[ "$actual" == "$expected" ]]
  }

  _fv_toolchain_fetch_verified() {
    local url expected destination temporary
    url=$1
    expected=$2
    destination=$3
    temporary=$(mktemp "${destination}.tmp.XXXXXX") || return 1
    printf 'Downloading pinned formal verification tool: %s\n' "$url" >&2
    if ! curl --fail --location --silent --show-error --retry 3 --output "$temporary" "$url"; then
      rm -f -- "$temporary"
      return 1
    fi
    if ! _fv_toolchain_sha256_matches "$expected" "$temporary"; then
      printf 'SHA-256 verification failed for download: %s\n' "$url" >&2
      rm -f -- "$temporary"
      return 1
    fi
    if ! mv -f -- "$temporary" "$destination"; then
      rm -f -- "$temporary"
      return 1
    fi
  }

  if [[ ! -f "$archive_path" ]] || ! _fv_toolchain_sha256_matches "$jdk_sha" "$archive_path"; then
    _fv_toolchain_fetch_verified "$jdk_url" "$jdk_sha" "$archive_path" || return 1
  fi
  # Verify cached archives too, immediately before extraction.
  if ! _fv_toolchain_sha256_matches "$jdk_sha" "$archive_path"; then
    printf 'cached JDK archive failed SHA-256 verification: %s\n' "$archive_path" >&2
    return 1
  fi

  _fv_toolchain_verify_jdk() {
    local release version implementation
    [[ -x "$jdk_root/bin/java" && -r "$jdk_root/release" ]] || return 1
    release="$jdk_root/release"
    version=$(sed -n 's/^JAVA_VERSION="\([^"]*\)"$/\1/p' "$release") || return 1
    implementation=$(sed -n 's/^IMPLEMENTOR="\([^"]*\)"$/\1/p' "$release") || return 1
    [[ "$version" == "${jdk_version%+*}" && "$implementation" == "Eclipse Adoptium" ]] || return 1
    java_output=$("$jdk_root/bin/java" -version 2>&1) || return 1
    [[ "$java_output" == *"${jdk_version%+*}"* ]]
  }

  if ! _fv_toolchain_verify_jdk; then
    if [[ -e "$jdk_root" ]]; then
      printf 'existing pinned JDK directory is incomplete or has the wrong version: %s\n' "$jdk_root" >&2
      return 1
    fi
    stage=$(mktemp -d "$tool_root/.jdk-install.XXXXXX") || return 1
    if ! tar -xzf "$archive_path" -C "$stage"; then
      rm -rf -- "$stage"
      return 1
    fi
    candidate=
    for candidate in "$stage"/*; do
      if [[ -x "$candidate/bin/java" ]]; then
        break
      fi
    done
    if [[ -z "$candidate" || ! -x "$candidate/bin/java" ]] || ! _fv_toolchain_verify_candidate_jdk "$candidate" "${jdk_version%+*}"; then
      printf 'verified JDK archive did not contain the expected pinned Temurin layout.\n' >&2
      rm -rf -- "$stage"
      return 1
    fi
    if ! mv -- "$candidate" "$jdk_root"; then
      rm -rf -- "$stage"
      return 1
    fi
    rm -rf -- "$stage"
    if ! _fv_toolchain_verify_jdk; then
      printf 'installed JDK failed its version check: %s\n' "$jdk_root" >&2
      return 1
    fi
  fi

  if [[ ! -f "$jar_path" ]] || ! _fv_toolchain_sha256_matches "$tla_sha" "$jar_path"; then
    _fv_toolchain_fetch_verified "$tla_url" "$tla_sha" "$jar_path" || return 1
  fi
  # Always recheck the cached TLA+ jar before making it available to the runner.
  if ! _fv_toolchain_sha256_matches "$tla_sha" "$jar_path"; then
    printf 'cached TLA+ jar failed SHA-256 verification: %s\n' "$jar_path" >&2
    return 1
  fi

  FV_JAVA="$jdk_root/bin/java"
  FV_TLA_JAR="$jar_path"
  export FV_JAVA FV_TLA_JAR

}

# Verify candidate installs before moving them into the pinned location.
_fv_toolchain_verify_candidate_jdk() {
  local root release version implementation java_output expected_release
  root=$1
  expected_release=$2
  [[ -x "$root/bin/java" && -r "$root/release" ]] || return 1
  release="$root/release"
  version=$(sed -n 's/^JAVA_VERSION="\([^"]*\)"$/\1/p' "$release") || return 1
  implementation=$(sed -n 's/^IMPLEMENTOR="\([^"]*\)"$/\1/p' "$release") || return 1
  [[ "$version" == "$expected_release" && "$implementation" == "Eclipse Adoptium" ]] || return 1
  java_output=$("$root/bin/java" -version 2>&1) || return 1
  [[ "$java_output" == *"$expected_release"* ]]
}

if _fv_toolchain_init; then
  _fv_toolchain_status=0
else
  _fv_toolchain_status=$?
fi
unset -f _fv_toolchain_init _fv_toolchain_sha256_matches _fv_toolchain_fetch_verified
unset -f _fv_toolchain_verify_jdk _fv_toolchain_verify_candidate_jdk
if (( _fv_toolchain_status != 0 )); then
  unset _fv_toolchain_status
  return 1
fi
unset _fv_toolchain_status
