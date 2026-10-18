#!/usr/bin/env bash
# One entry point for baseline findings, positive checks, seeded failures,
# restored checks, and the repository's original unittest suite.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source formal/toolchain.sh
exec python3 formal/run_checks.py "$@"
