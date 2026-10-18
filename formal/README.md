# Watcher completion and crash verification

F1-F3 from public commit `914f961e2f1118615a5a45edb9b80873ba3f0b89`
are fixed in the guarded implementation. Historical counterexamples remain in
[evidence/baseline](evidence/baseline) and [evidence/cold-run](evidence/cold-run).
Those logs describe the old product, not the current result.

## Properties and HTTP contract

**Exactly once per eligible completed turn at logical enqueue:** each newly
observed completed turn needing attention queues once; touching, rotating,
rewriting or restoring its unchanged transcript does not queue it again.

**No loss across process crashes:** an eligible completion remains discoverable
or durably queued until the receiver accepts it, including crashes between the
daybook append, state replacement and delivery.

“Eligible” means `t3_done_notify.py:needs_attention` is true, a local file was
discovered after the initial index baseline, its complete turn remains readable,
and debounce observations occur. Historic first-index completions and ordinary
non-attention completions are not webhook obligations. Completion enqueues
exclude separate stalled alerts. Physical HTTP receipts, logical enqueue,
receiver effects, and daybook appends are separate observations.

Delivery is **at-least-once**. The unchanged `AtMostOneReceipt` property still
has a counterexample: a receiver accepts a POST, its response is lost (or the
watcher crashes before saving acknowledgement), and the same POST is retried.
Exactly-once HTTP delivery cannot be guaranteed by the sender. F2 adds a stable
`event_id` in JSON and matching `Idempotency-Key` header. A receiver must durably
dedupe on this identity to obtain at-most-one application effect. The suite
retains the physical-receipt failure as an explicitly expected limitation and
separately checks stable identity and the deduping receiver's effect count; it
does not rename physical receipt into logical delivery or weaken that property.

## Run everything

From the repository root with `requirements.txt` installed:

```sh
./formal/run.sh
```

Exit **0** means all fixed properties, replays, expected mutation failures,
restored checks and unittests matched. Exit **1** means an unexpected result,
stale source hash, replay failure, timeout or failed test. Individual failing TLC
checks return 12 (safety) or 13 (liveness). The runner explicitly expects exit 12
for the unchanged physical HTTP receipt diagnostic. All other unmutated models
must pass. No live notifier, real webhook or T3 server is used.

Fresh logs, exact commands, wall times, generated/distinct state counts and input
hashes go to ignored `formal/.runs/<UTC timestamp>-<pid>/`. Preserve a review with:

```sh
./formal/run.sh --evidence formal/evidence/fixed-review
```

Use a new empty evidence directory. Local account paths in exported logs and
command arguments are normalized to `$HOME`; tool/source hashes, traces and
results are preserved. Java patch releases use `21.0.12-patch1` in exported
logs, distinguishing a software version from an address. Reconstruct literal
upstream spellings from the structured lock when replaying logged commands.
Historical logs have the same path and version normalization.

Prerequisites: Linux x86_64, Bash, curl, tar, sha256sum, Python 3.10+ and
`websockets>=15,<16` from `requirements.txt`. `toolchain.sh` verifies cached
archives/jars and, if needed, downloads checksum-pinned Temurin **21.0.12-patch1+1**
and TLA+ **1.8.0** (2026-10-15 prerelease; TLC **2026.10.15.145915**) only under
`~/.local/share/fv-tools/t3-done-notify/`. `toolchain.lock.json` schema 2
stores release components `[21, 0, 12, 1]`, build 1, platform and release base
URL. `toolchain.sh` constructs the upstream archive name and download URL; the
JDK checksum source is that URL plus `.sha256.txt`. Both SHA-256 pins are
unchanged. This representation keeps version metadata distinct from address
literals without introducing exceptions to publication checks. No tool binaries belong in Git. TLC is exhaustive within
each finite model: breadth-first, one worker, seed 1, fingerprint polynomial 0,
1 GiB Java heap. It is not simulation or an unbounded refinement proof.

## F1 fixed: per-session index commit

Old trace: A and B appeared after the empty baseline; `_track_files` published
both signatures in RAM; saving A saved B's index without B's observation; a
crash made unchanged B permanently invisible to subsequent polls.

`t3_done_notify.py:_track_files` now stages signatures without changing state.
`poll_local_sessions` passes only that session's staged file entries to
`_commit_observation`, which puts them in the same candidate as its record and
outbox event. `save_state` atomically replaces this candidate before RAM
publication. The final index save happens only after all observations finish.
The first historic baseline is deliberately still ignored. Loose/unparseable
files and unstable reads retain their previous semantics. A failed observation
cannot advance another session's committed signature.

`Crash.tla:Scan` leaves memory.files unchanged; `BuildObservation` adds only the
current session alongside its record/queue. `NoStrandedCompletion` and
`EventuallyDelivered` are unchanged and now pass. `check_crashes.py:index-loss`
crashes after A's first real state replacement, checks B is not yet indexed,
restarts and observes one queued event for each. The unittest also covers B
already having a committed record for an older turn, guarding against fixing
only the absent-record case. Old already-stranded state cannot be reconstructed
unambiguously; this fix prevents the write ordering defect going forward.

## F2 fixed: stable retry identity, explicit receiver dedupe

`t3_done_notify.py:event` hashes source, session/thread id, reason, and turn
identity (or the updated timestamp for T3 and stalled alerts). This does not
change existing payload fields. Local turn hashes already namespace the source
and transcript session id, independently of filename. New turns sharing a clock
timestamp remain distinct when the transcript supplies distinct turn identities.
`deliver` sends `event_id` as `Idempotency-Key` on every attempt. `flush_outbox`
upgrades legacy queued payloads deterministically, preferring their persisted
completion key; direct legacy `deliver` calls get a deterministic fallback too.
No random/request-time value enters the identity. It survives response loss,
acknowledgement crashes and restart. The default T3-only behavior is unchanged
apart from the added field/header.

`Crash.tla:Pick` chooses the same request identity per event; `HttpAccept`
records physical receipts and the identities a deduping receiver has seen.
`StableEventIdentity` and `AtMostOneEffect` check the one-key/one-effect contract
in `CrashIdempotent.cfg`. The receiver projection assumes atomic, durable dedupe
on the supplied header. `AtMostOneReceipt` remains unchanged and fails in
`CrashDuplicate.cfg`. `check_crashes.py:delivery-duplicate` replays both the
acknowledgement-crash and real `deliver` lost-response paths and checks stable
body/header identity. The regression also exercises all three sources and
reason/turn identity separation. Legacy events with no turn identity fall back
to source/id/reason/timestamp; previously discarded identities cannot be restored.

## F3 fixed: persistent content identities without eviction

Old trace: A was delivered, then its transcript renamed without byte changes;
pruning A and 2,048 other vanished records evicted A's bounded tombstone before
discovery at the new path. Two stopped observations re-enqueued A.

`t3_done_notify.py:_remember_local_completion` now retains every distinct
content key in `_local_completed`, without a cap. `_session_thread` derives keys
from source, transcript session id, and turn identity, never the path.
`_commit_observation` persists the key with the session record and outbox;
`_prune_local_sessions` preserves it when the live path disappears;
`poll_local_sessions` suppresses known keys. The existing list format migrates
without conversion. Old evicted history cannot be recovered, but every retained
and newly committed identity is preserved indefinitely. State grows with distinct
completed turns; there is no time, count or file-count eviction policy.

`Dedupe.tla:Remember` appends without eviction when `BoundHistory=FALSE`.
`ScanAndCache` stages only; `Commit` advances the session's index and record
together. The unchanged `NoDuplicateEnqueue` passes for rotation and for rollback
with the same session/turn/path/metadata bounds as before. Python replays cover
same-byte touch, rewrite, inode replacement, simultaneous renames, and A renamed
after 2,048 valid delivered records for vanished sessions. This constructed
prefix avoids pretending to execute thousands of full session lifecycles.

## Models, boundaries and assumptions

`Crash.tla` separates scanning, transition computation, daybook append, candidate
construction, temporary-file write/fsync, atomic replacement, RAM publication,
remote acceptance, HTTP response and saved acknowledgement. Each action cites
`t3_done_notify.py:function`. RAM-only sequences are stutter reductions: a crash
inside them has the same durable result as at their boundary. An incomplete
temporary file is never loaded; only `os.replace` switches durable state. Partial
daybook bytes have no effect on notification recovery and are omitted.

- `Crash.cfg`: two sessions, one crash, one failed/lost-response request;
  unchanged `NoStrandedCompletion` safety property.
- `CrashLiveness.cfg`: two sessions, one crash, zero failed requests;
  unchanged `EventuallyDelivered` under weak fairness of polling.
- `CrashDurable.cfg`: two sessions, one crash, one failed request;
  unchanged `DurableOutbox` for already-committed completions.
- `CrashRetry.cfg`: one session, one crash, five failed requests;
  durable queue safety and eventual delivery, including all five internal
  attempts, failure-only batches, a later poll and acknowledgement persistence.
- `CrashDuplicate.cfg`: one session, one crash, one failed request;
  unchanged physical `AtMostOneReceipt`, expected to fail.
- `CrashIdempotent.cfg`: same bounds as `CrashDuplicate.cfg`; stable identity
  and at-most-one effect for a receiver that durably dedupes.

The two-session model fixes scan order; outbox order is nondeterministic.
Records represent absent, pending, or completed. Later polls are past debounce.
Distinct completion keys stand for collision-free content identities. Physical
receipts and daybook counters saturate at two. Identity sets contain at most two
symbols for the unstable-key mutant; actual keys are hashes. Fixed files stay
readable and the first index already exists. Finite faults plus fair scheduling
and eventual healthy webhook responses imply recovery.

All Dedupe configurations retain two sessions, three turn keys (two for one
session), two paths per session and two metadata classes. Source changes occur
between polls; discovery may visit sessions in either order, pruning follows
record insertion order, and the per-poll completion cache grows after commits.
`Dedupe.cfg` allows rotation; `DedupeRetained.cfg` disables rotation/rollback;
`DedupeHistory.cfg` allows both. `Capacity` is consulted **only** by seeded
`BoundHistory=TRUE` mutations. Finite key universes bound model exploration,
not the production history. There are no crashes/I/O failures here; commits
linearize at successful `os.replace`. The crash family splits that boundary.
Legacy migration, multiple paths for one session and unstable reads remain
outside this TLA projection; Python tests exercise those implementation paths.

`check_transition.py` retains 24 actual transition boundary cases: debounce ages
absent/9/10/11 seconds at a 10-second threshold with active false/true; first
stopped observation at zero debounce; active resumption clearing pending; stall
threshold 2700 seconds with absent/equal/different timestamps; invalid/fresh
stamps; repeated/newer activity. These are executable boundaries, not a proof
for every clock value or every stall behavior.

## Mutation proof and maintenance

`run_checks.py` changes actual product copies in temporary directories, never
the checkout. Every mutation must first fail the unchanged hash guard, then
fail both TLC and real-code replay after its scratch hash/config is mirrored.
Exact restoration must restore all passes. Patches and raw traces are saved.

1. Publish staged batch index early (`BatchIndex=TRUE`):
   `NoStrandedCompletion` and the actual index-crash replay fail (F1).
2. Use a new header key per `deliver` call (`StableIds=FALSE`):
   `StableEventIdentity` and the body/header identity replay fail (F2).
3. Evict history beyond one key (`BoundHistory=TRUE`, capacity one):
   `NoDuplicateEnqueue` and simultaneous rotation replay fail (F3).
4. Remove `candidate["_outbox"].append(queued)` (`DropQueue=TRUE`):
   unchanged `DurableOutbox` and durable-outbox replay fail.
5. Replace `if already_completed:` with `if False:` (`RespectDedupe=FALSE`):
   unchanged `NoDuplicateEnqueue` and ordinary rename replay fail.

The [verify-watcher skill](../.claude/skills/verify-watcher/SKILL.md) is also
packaged as an identical regular-directory copy under
`.agents/skills/verify-watcher/`. The runner checks that both copies match. `check_guard.py` compares the whole
product file with `guarded.json`. Review affected actions/callers before changing
the hash. Every run records hashes of its model/config/runner inputs. Historical
suite provenance follows the pinned
[pstack v0.15.27 meta-skill](https://raw.githubusercontent.com/cursor/plugins/0148b78c56c72a1b8412055913c79f767095b217/pstack/skills/create-formal-verification-skill/SKILL.md).

## Limits

These are bounded model checks plus concrete Python replays, not a refinement
proof or a composed proof of every crash/rotation interleaving. One watcher,
process crashes and an intact local filesystem are assumed. Power loss, missing
directory fsync, corruption, ENOSPC, simultaneous writers, permission errors,
arbitrary parser behaviors, hash collisions, clock rollback and real receiver
behavior are outside scope. The latest-snapshot API may hide turns beginning
and ending between observations. Attention heuristics and all protocol adapter
versions are not proved. Infinite faults cannot guarantee eventual delivery.
Daybook appends may duplicate after a crash. Sender-only exactly-once HTTP is
not claimed; receivers must implement persistent idempotency.
