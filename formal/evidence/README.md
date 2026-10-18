# Publication follow-up verification

[publication-review/summary.json](publication-review/summary.json) records a new
`./formal/run.sh --evidence formal/evidence/publication-review` run: **exit 0**,
**52 checks matched**, no unexpected failures, and **83 tests OK**. All five
seeded product mutations still fail both TLC and actual-code replay; restoring
each copy restores the passes. F1-F3 regressions again fail on public main and
pass after the fixes: [before](publication-regressions/before.log) and
[after](publication-regressions/after.log). Product code and regression tests
are unchanged from the prior fixed review.

The publication recipe's documentation line references now match the actual
README commands. The agent skill is a regular directory copy, checked against
the canonical skill by the runner. The lock represents JDK version/build/platform
as separate fields; constructing the archive and URL yields exactly the prior
upstream artifacts and unchanged SHA-256 pins. Warm cache verification, cold
extraction and invalid-metadata rejection were checked. Published logs use
`21.0.12-patch1` to separate the version's patch component from address syntax;
historical logs use the same normalization, retaining original input hashes.
No publication approvals or exclusions were added. The separate squash audit
and its private reports remain outside the repository.

---

# Fixed F1-F3 verification

The fixed review is [fixed-final/summary.json](fixed-final/summary.json):
`./formal/run.sh --evidence formal/evidence/fixed-final` exited **0** with all
**52** recorded checks matched and no unexpected failures. Five real product
mutations failed both their named modeled property and Python replay, then
passed after exact restoration. The unchanged physical `AtMostOneReceipt`
property still fails as the documented HTTP limitation. All other unmutated
model checks pass. Per-check state counts, commands, timings and input hashes
(including the actual unittest sources and requirements) are preserved.

Guarded product SHA-256: `b3e9c729731cba1d3fb48329c398f240a83ce40e8657f96d28f20fa5804bd204`.
Runner wall time: **40.175247 seconds**.

The venv was created with `python3 -m venv .venv` and populated only with
`python3 -m pip install -r requirements.txt`; `pip freeze` reported only
`websockets==15.0.1`. The documented `python3 -m unittest discover -s tests -q`
command ran through this venv in [existing-tests.log](fixed-final/existing-tests.log):
**83 = 80 + 3 tests, OK; zero failures/errors**. Installer tests use temporary
HOME/XDG directories and stubbed systemctl/launchctl.

The three new tests in `tests/test_fv_fixes.py` were also run with public-main
`t3_done_notify.py` from `914f961`, using a separate temporary copy and the same
new test sources. [before.log](fix-regressions/before.log) reports **3 tests,
6 subtest failures, exit 1**; [after.log](fix-regressions/after.log) reports
**3 tests, OK, exit 0**. F1 fails on advancing B's index for both absent and
previously committed B; F2 fails for every source's missing event identity;
F3 reproduces an actual second notification after unchanged rename with 2,048
other completed records. Fixed replays preserve one event per completed turn.

Pre-commit scans using the requested commands both exited **0**, no leaks:
[gitleaks history](fix-regressions/gitleaks-history.log) and
[gitleaks filesystem](fix-regressions/gitleaks-files.log). Transcript-content
hashes are not printed by the failing regression assertion. Exploratory outputs
were preserved outside the clone; no detector exceptions or suppressions were
added. No live notifier, release check, push, PR or service action was used.

Completion history is uncapped and grows with distinct completed turns. Existing
pre-fix stranded/evicted history cannot be reconstructed. Delivery remains
at-least-once; receivers must durably dedupe on `event_id` / `Idempotency-Key`.
See [the current model correspondence and limits](../README.md).

---

> Historical pre-fix evidence. Local account paths were normalized to `$REPO`
> and `$HOME` for publication; Java patch versions use `21.0.12-patch1`.
> Trace states, results and original input hashes are retained.
> Current fixes and verification are documented in [../README.md](../README.md).

# Verification evidence — 2026-10-18

Baseline product commit: `914f961e2f1118615a5a45edb9b80873ba3f0b89`.
Guarded product SHA-256:
`150a7304f317847d1098b963c306cdd622fd582a19349d85b327dce62434cfb0`.
The branch adds verification artifacts only; the product and original tests are
unchanged. No push, service operation or live webhook was used.

## Exact baseline run

Working directory: `$REPO`.

```sh
./formal/run.sh --evidence formal/evidence/baseline
```

Result: **exit 2**, all 30 recorded outcomes matched, no unexpected failures.
The two end-to-end promises remain **false**. Runner wall time was
**30.221734 seconds**, excluding the shell's pinned-tool cache verification.
Per-check durations below include subprocess startup. A failed safety check's
state count is the explored prefix at its counterexample, not a complete proof.

Tools: Eclipse Temurin **21.0.12-patch1+1**; TLA+ jar **1.8.0**, TLC build
**2026.10.15.145915**; Python **3.14.4**; websockets **15.0.1**.
All TLC checks used one worker, seed 1, fingerprint polynomial 0, breadth-first
search and `-Xmx1g`. Pinned downloads and SHA-256 provenance are in
[toolchain.lock.json](../toolchain.lock.json). Exact expanded commands, exit codes
and raw output are in each log. [inputs.json](baseline/inputs.json) records the
exact source, spec, config, skill and runner hashes used.

Bounds: Crash models have one or two sessions, at most one process crash, and
zero, one or five failed/lost-response HTTP attempts. Dedupe models have two
sessions, three turn keys, capacity one or three, two paths and two metadata
classes per session. The separate actual-code witness exercises the real 2048
limit using a constructed reachable prefix plus real prune/discovery polls.
Configuration-by-configuration assumptions and exclusions are in
[the verification README](../README.md). These are separate bounded projections,
not an unbounded proof or a proof of all combined crash/rotation interleavings.

| Check | Result | Generated / distinct states | Seconds |
| --- | --- | ---: | ---: |
| [baseline-index-loss](baseline/baseline-index-loss.log) | FAIL (expected finding or mutation) | 20 / 15 | 0.487138 |
| [baseline-no-loss-liveness](baseline/baseline-no-loss-liveness.log) | FAIL (expected finding or mutation) | 883 / 768 | 0.491260 |
| [baseline-durable-outbox](baseline/baseline-durable-outbox.log) | PASS | 3099 / 2610 | 0.471274 |
| [baseline-five-retries](baseline/baseline-five-retries.log) | PASS | 2460 / 2003 | 0.619594 |
| [baseline-webhook-duplicate](baseline/baseline-webhook-duplicate.log) | FAIL (expected finding or mutation) | 100 / 82 | 0.415121 |
| [baseline-rotation-eviction](baseline/baseline-rotation-eviction.log) | FAIL (expected finding or mutation) | 190423 / 89605 | 1.700273 |
| [baseline-retained-record](baseline/baseline-retained-record.log) | PASS | 8891 / 6458 | 0.631770 |
| [baseline-retained-history](baseline/baseline-retained-history.log) | PASS | 472967 / 187730 | 2.708377 |
| [seed-drop-outbox-model-fails](baseline/seed-drop-outbox-model-fails.log) | FAIL (expected finding or mutation) | 49 / 38 | 0.430987 |
| [seed-drop-outbox-revert-model](baseline/seed-drop-outbox-revert-model.log) | PASS | 2460 / 2003 | 0.592376 |
| [seed-bypass-dedupe-model-fails](baseline/seed-bypass-dedupe-model-fails.log) | FAIL (expected finding or mutation) | 133644 / 59965 | 1.244266 |
| [seed-bypass-dedupe-revert-model](baseline/seed-bypass-dedupe-revert-model.log) | PASS | 472967 / 187730 | 2.591171 |

The [machine-readable summary](baseline/summary.json) also includes guards,
actual-code replays, versions and original tests. The four passing baseline
component checks retain their narrower scope; they do not replace the failed
end-to-end claims.

## Historical baseline counterexamples in plain steps

**F1 — lost completion after a crash**

1. Start from a persisted empty index; completed attention turns A and B appear.
2. `_track_files` indexes both before the per-session loop.
3. Saving A's pending debounce record also saves B's index entry.
4. Crash after `os.replace`, before B is observed.
5. Restart: B is indexed but has no record. Its unchanged bytes make `changed`
   false, so every later poll skips B. No notification is queued for B.

Evidence: [safety trace](baseline/baseline-index-loss.log),
[infinite polling counterexample](baseline/baseline-no-loss-liveness.log),
[real-code crash replay](baseline/baseline-product-crashes.log).

**F2 — allowed duplicate physical delivery**

1. A saved outbox entry is accepted by the webhook.
2. The response is lost, or the process crashes before saving queue removal.
3. A retry is accepted again. The code has no receiver idempotency protocol.

Evidence: [TLC trace](baseline/baseline-webhook-duplicate.log) and
[actual `deliver` lost-response/local-ack crash replay](baseline/baseline-product-crashes.log).
This is the code's allowed at-least-once window; strict exactly-once still fails.

**F3 — unchanged transcript re-notified after rotation and eviction**

1. Deliver A. Other completed sessions evict A's bounded tombstone, while A's
   live record still holds `completed_turn`.
2. Rename A without changing its bytes; other completed transcripts vanish.
3. Pruning remembers A then deletes its old-path record. Pruning the other
   records fills the tombstone list and evicts A again.
4. The subsequent completion-cache snapshot cannot recognize A at its new path.
5. One stopped observation starts debounce; the next queues A again.

Evidence: [capacity-one TLC trace](baseline/baseline-rotation-eviction.log),
[actual capacity-one and capacity-2048 replays](baseline/baseline-product-dedupe.log).
The 2048-prefix fixture is stated explicitly in the harness and README; it does
not claim to have executed 2048 complete lifecycles. Ordinary same-path touches,
rewrites and inode replacement remain protected by the live record in the
[passing replay](baseline/baseline-product-dedupe.log).

## Actual product bugs seeded, mirrored and restored

**Drop the outbox append.** [Exact source patch](baseline/seed-drop-outbox.patch)
removes `candidate["_outbox"].append(queued)`; the
[model mirror](baseline/seed-drop-outbox-mirrored.cfg) sets `DropQueue = TRUE`.

1. Observe completion, append its daybook entry and save a done record.
2. With the append removed, no outbox entry accompanies the completed marker.
3. The receiver has accepted nothing. After restart the turn is considered done
   and has no queued payload: `DurableOutbox` fails.

The [unchanged hash guard rejects it](baseline/seed-drop-outbox-guard-rejects.log).
After the scratch hash is deliberately reviewed,
[TLC fails](baseline/seed-drop-outbox-model-fails.log) and
[the actual code fails](baseline/seed-drop-outbox-product-fails.log).
Exact source/config restoration yields
[a passing model](baseline/seed-drop-outbox-revert-model.log) and
[a passing code replay](baseline/seed-drop-outbox-revert-product.log).

**Bypass completed-turn suppression.**
[Exact source patch](baseline/seed-bypass-dedupe.patch) replaces
`if already_completed:` with `if False:`; the
[model mirror](baseline/seed-bypass-dedupe-mirrored.cfg) sets
`RespectDedupe = FALSE` in the otherwise-passing retained-history configuration.

1. Deliver A once and keep its completion key.
2. Rename its unchanged transcript. Pruning/discovery makes it look newly seen,
   but the remembered key should suppress another completion.
3. The mutant ignores that suppression. Debounce starts again and a second
   observation queues A a second time: `NoDuplicateEnqueue` fails.

The [unchanged hash guard rejects it](baseline/seed-bypass-dedupe-guard-rejects.log).
After the scratch hash is deliberately reviewed,
[TLC fails](baseline/seed-bypass-dedupe-model-fails.log) and
[the actual code fails](baseline/seed-bypass-dedupe-product-fails.log).
Exact source/config restoration yields
[a passing model](baseline/seed-bypass-dedupe-revert-model.log) and
[a passing code replay](baseline/seed-bypass-dedupe-revert-product.log).
The temporary mutated copies are removed after restoration; their exact patches,
mirrored configurations and complete logs are retained here.

## Original tests and transition boundaries

[The unchanged existing suite](baseline/existing-tests.log) reports **80 tests,
OK**. Its command was `python3 -m unittest discover -s tests -q` (the log records
the resolved Python executable). [Twenty-four actual transition calls](baseline/baseline-product-transition.log)
check debounce/stall boundary partitions; these are bounded executable checks,
not a general clock or stall proof. [The final guard](baseline/final-guard.log)
confirms the product SHA-256 still matches the original source.

## Independent cold skill run

A fresh agent read the generated skill and its referenced code/documentation,
then ran the exact command `./formal/run.sh` from the repository root. It returned
**exit 2** after **30.635959 seconds**, with all 30 expected outcomes reproduced
and no unexpected failures. Both seeded mutations failed in TLC and actual code;
both restored checks passed. The original suite again reported **80 tests, OK**.

The original output directory was
`formal/.runs/20261018T213950Z-965214`; its 36 files were copied with local path normalization
to [cold-run/](cold-run/). The preserved [summary](cold-run/summary.json) includes
every command, count and runtime. Its [input hashes](cold-run/inputs.json) match
the baseline run and the final verification inputs exactly. The agent's
independent, step-by-step review is in [cold-review.md](cold-review.md).
