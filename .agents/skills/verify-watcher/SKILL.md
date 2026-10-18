---
name: verify-watcher
description: Verify t3-done-notify watcher transitions, completion dedupe, file indexing and crash/outbox delivery when changing t3_done_notify.py, its persistence order, or its completion identity rules.
---

# Verify watcher completion and recovery

Use this skill from the repository root when changing the guarded file
`t3_done_notify.py`, especially `transition`, `_commit_observation`,
`_remember_local_completion`, `event`, `_signature_changed`,
`_index_entry`, `_track_files`, `_prune_local_sessions`, `poll_local_sessions`,
`save_state`, `flush_outbox` or `deliver`. These are the kept-in-step triggers.
The whole file is guarded, including its parser and caller dependencies, so
changes elsewhere in it also require a review rather than a silent hash refresh.

## What you are checking

1. Each eligible completed turn should produce one notification, with no replay
   from unchanged-content touches/rotations/rewrites, without arbitrary history eviction.
   A violation either misses human action or repeats a stale alert.
2. A completion should remain discoverable or queued across process crashes until
   it is accepted by the webhook. A violation silently loses a request for help.

Read `formal/README.md` before interpreting results. F1-F3 are fixed; retain
their seeded regressions and the explicit HTTP physical-duplicate limitation.
Passing component checks remain bounded results. Logical enqueues, remote POST receipts, stalled alerts and daybook
appends are different events. Do not call their combined behavior exactly-once.

## Run it cold

1. Confirm you are in this repository and read `formal/guarded.json` and the
   cited product functions. Do not use the installed watcher or live session trees.
2. Run this exact command from the root:

   ```sh
   ./formal/run.sh
   ```

   The runner requires Linux x86_64, Bash, curl, tar, sha256sum, Python 3.10+ and
   the existing `requirements.txt` dependency. It downloads only pinned,
   checksum-verified Temurin 21.0.12-patch1+1 and TLA+ 1.8.0 to
   `~/.local/share/fv-tools/t3-done-notify/`. No sudo, service changes or binaries
   in the repo. Exact hashes/provenance are in `formal/toolchain.lock.json`.
3. Read the printed evidence directory's `summary.json` and logs. Exit **0**
   means all fixed checks and expected mutation/HTTP-limit failures matched. Exit **1**
   means an unexpected failure or a stale hash. A TLC failure must name the
   intended invariant/property, not merely fail to parse or run. The runner
   enforces TLC exit codes and diagnostic text. Do not claim sender-only exactly-once HTTP delivery.
4. Confirm that every seeded product mutation fails the hash guard, then fails
   the corresponding modeled property and actual-code replay after its scratch
   model/hash are deliberately updated. Confirm its restored checks pass. The
   runner performs these steps on private copies of the actual source and saves
   the exact product patches, mirrored configurations and readable raw traces.
5. Confirm `existing-tests.log` reports all 83 unittests passing and
   `final-guard.log` proves the checkout product hash did not change. Report the
   results in code terms using the described fixes in `formal/README.md`.

That single command is the cold-follow acceptance procedure: it includes the
baseline, real-code mutations, model mirrors, guard rejection, restored checks
and regression tests. It does not require manual edits or outside services.

## Review a guarded-code change

`python3 formal/check_guard.py` is the cheap trigger. If it fails, do not just
update `formal/guarded.json` to silence it.

1. Read the changed functions and their callers. Map each affected state field,
   guard, persistent write and delivery side effect to the cited actions in
   `formal/Crash.tla` and `formal/Dedupe.tla`. If no action models it, identify
   and document that scope boundary before claiming coverage.
2. Mirror the implementation, including a suspected bug, before running its
   property. Keep `os.replace`, remote acceptance and local acknowledgement
   separate. A Python function is not automatically atomic. RAM-only stutter
   reductions require the documented crash-equivalence argument.
3. Preserve the strong properties. For a TLC counterexample, replay its steps in
   `formal/check_crashes.py` or `formal/check_dedupe.py` against the actual changed
   product. A reproducible code trace is a finding. An impossible model step is
   a modeling error: correct the model and explain the discrepancy. Never change
   a property or bound merely to make a counterexample disappear.
4. Only after that review, record the reviewed product hash using:

   ```sh
   python3 - <<'PY'
   import hashlib, json
   from pathlib import Path
   path = Path('formal/guarded.json')
   recorded = json.loads(path.read_text())
   recorded['sha256']['t3_done_notify.py'] = hashlib.sha256(
       Path('t3_done_notify.py').read_bytes()).hexdigest()
   path.write_text(json.dumps(recorded, indent=2) + '\n')
   PY
   ```

5. Rerun `./formal/run.sh`. If an intended fix changes a recorded finding, revise
   the runner's expected result **only with** the code replay, model review and
   updated readable explanation. Keep a real failing mutation for each property.
   Use the original suite to verify compatibility. Product fixes, commits and
   publication still require authorization from the current task.
6. Preserve a new review's evidence with
   `./formal/run.sh --evidence formal/evidence/new-review` using a new empty
   directory (choose a different name if it already exists). Record the exact
   source/model hashes, command, tool versions, bounds, counts, runtimes and
   limitations. Never overwrite old evidence or commit tool caches.

## Boundaries to retain

The finite models cover small numbers of completion keys, finite process
crashes/failures, one watcher and fair recovery. A
real-code rename regression crosses the former 2048-entry cap; production
completion history is now uncapped. Fixed-file recovery
assumes the source remains readable and that a later poll can finish debounce.
Power loss, two concurrent watcher writers, filesystem corruption, arbitrary
parser behavior, hash collisions and infinite failure are outside scope. See
`formal/README.md` for all per-configuration bounds and counterexamples.

This skill was generated under the pinned pstack v0.15.27
[create-formal-verification-skill](https://raw.githubusercontent.com/cursor/plugins/0148b78c56c72a1b8412055913c79f767095b217/pstack/skills/create-formal-verification-skill/SKILL.md).
