#!/usr/bin/env python3
"""Check fixed safety/liveness, explicit HTTP limits, and seeded regressions."""
import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
FORMAL = ROOT / "formal"
PYTHON = sys.executable


def portable(value):
    """Keep reproducible command structure without exporting local account paths."""
    java_release = ".".join(map(str, (21, 0, 12, 1)))
    return str(value).replace(str(Path.home()), "$HOME").replace(
        java_release, "21.0.12-patch1")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Run:
    def __init__(self, output):
        self.output = output
        self.rows = []
        self.problems = []

    def command(self, name, argv, expected=0, marker=None, cwd=ROOT):
        started = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=120)
            code, body = result.returncode, result.stdout
        except subprocess.TimeoutExpired as exc:
            code, body = 124, str(exc.stdout or "") + "\nTIMEOUT after 120 seconds\n"
        elapsed = time.monotonic() - started
        matched = code == expected and (marker is None or marker in body)
        text = (f"Command: {shlex.join(map(str, argv))}\nWorking directory: {cwd}\n"
                f"Expected exit: {expected}\n{body}\nActual exit: {code}\n"
                f"Wall runtime seconds: {elapsed:.6f}\n"
                f"Recorded expectation matched: {matched}\n")
        (self.output / f"{name}.log").write_text(portable(text))
        row = {"check": name, "command": [portable(arg) for arg in argv], "exit": code,
               "expected_exit": expected, "wall_seconds": round(elapsed, 6),
               "matched": matched}
        # Preserve TLC's own generated/distinct counts without interpreting a
        # counterexample prefix as a completed exhaustive exploration.
        row["tlc_statistics"] = [line for line in body.splitlines()
                                 if "states generated," in line or
                                 "depth of the complete state graph" in line]
        self.rows.append(row)
        label = "PASS" if expected == 0 else "EXPECTED FAILURE"
        print(f"{label if matched else 'UNEXPECTED RESULT'}: {name} "
              f"(exit {code}, {elapsed:.3f}s)", flush=True)
        if not matched:
            self.problems.append(name)
            print(body[-4000:], flush=True)
        return matched

    def tlc(self, name, module, config, expected=0, marker=None):
        states = ROOT / "formal/.runs" / f"states-{os.getpid()}-{name}"
        argv = [os.environ["FV_JAVA"], "-XX:+UseParallelGC", "-Xmx1g",
                "-cp", os.environ["FV_TLA_JAR"], "tlc2.TLC", "-workers", "1",
                "-seed", "1", "-fp", "0", "-noGenerateSpecTE", "-difftrace",
                "-metadir", str(states), "-config", str(config), str(module)]
        return self.command(name, argv, expected, marker)

    def mutation(self, name, old, new, module, config, cfg_old, cfg_new,
                 invariant, replay_script, replay_args, failure_marker):
        original = (ROOT / "t3_done_notify.py").read_text()
        if original.count(old) != 1:
            raise RuntimeError(f"{name}: mutation anchor changed; review skill/model")
        altered = original.replace(old, new, 1)
        (self.output / f"{name}.patch").write_text("".join(difflib.unified_diff(
            original.splitlines(keepends=True), altered.splitlines(keepends=True),
            fromfile="a/t3_done_notify.py", tofile="b/t3_done_notify.py")))
        # The scratch file IS a changed copy of the actual product implementation,
        # not a mocked implementation. Never mutate the checkout or its index.
        with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=FORMAL / ".runs") as tmp:
            scratch = Path(tmp)
            product = scratch / "t3_done_notify.py"
            product.write_text(altered)
            self.command(f"{name}-guard-rejects", [PYTHON, str(FORMAL / "check_guard.py"),
                         "--product", str(product)], 1, "changed since the model")
            reviewed = json.loads((FORMAL / "guarded.json").read_text())
            reviewed["sha256"]["t3_done_notify.py"] = digest(product)
            recorded = scratch / "guarded.json"
            recorded.write_text(json.dumps(reviewed, indent=2) + "\n")
            cfg_text = config.read_text()
            if cfg_text.count(cfg_old) != 1:
                raise RuntimeError(f"{name}: model mutation anchor changed")
            mutated_config = scratch / config.name
            mutated_config.write_text(cfg_text.replace(cfg_old, cfg_new, 1))
            (self.output / f"{name}-mirrored.cfg").write_text(mutated_config.read_text())
            self.command(f"{name}-guard-reviewed", [PYTHON, str(FORMAL / "check_guard.py"),
                         "--product", str(product), "--recorded", str(recorded)])
            self.tlc(f"{name}-model-fails", module, mutated_config, 12,
                     f"Invariant {invariant} is violated")
            self.command(f"{name}-product-fails", [PYTHON, str(replay_script),
                         "--product", str(product), *replay_args], 1, failure_marker)
            # Restore exactly, then rerun the same property and actual-code path.
            product.write_text(original)
            if digest(product) != digest(ROOT / "t3_done_notify.py"):
                raise RuntimeError("scratch product restoration did not match")
            self.command(f"{name}-revert-guard", [PYTHON, str(FORMAL / "check_guard.py"),
                         "--product", str(product)])
            self.tlc(f"{name}-revert-model", module, config)
            self.command(f"{name}-revert-product", [PYTHON, str(replay_script),
                         "--product", str(product), *replay_args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path,
                        help="New empty evidence directory; default formal/.runs/<timestamp>")
    args = parser.parse_args()
    (FORMAL / ".runs").mkdir(exist_ok=True)
    output = args.evidence or FORMAL / ".runs" / (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error(f"evidence directory must be empty: {output}")
    run = Run(output)
    started = time.monotonic()
    original_hash = digest(ROOT / "t3_done_notify.py")
    inputs = [ROOT / "t3_done_notify.py", *sorted(FORMAL.glob("*.tla")),
              *sorted(FORMAL.glob("*.cfg")), *sorted(FORMAL.glob("*.py")),
              FORMAL / "guarded.json", FORMAL / "toolchain.lock.json",
              FORMAL / "toolchain.sh", FORMAL / "run.sh", FORMAL / "README.md",
              ROOT / ".claude/skills/verify-watcher/SKILL.md",
              ROOT / ".agents/skills/verify-watcher/SKILL.md", ROOT / "README.md",
              ROOT / "requirements.txt", *sorted((ROOT / "tests").glob("test_*.py"))]
    inputs = [p for p in inputs if "_TTrace_" not in p.name]
    (output / "inputs.json").write_text(json.dumps(
        {str(p.relative_to(ROOT)): digest(p) for p in inputs}, indent=2) + "\n")
    print(f"Evidence: {output}", flush=True)
    try:
        if (ROOT / ".agents/skills/verify-watcher/SKILL.md").read_bytes() != (
                ROOT / ".claude/skills/verify-watcher/SKILL.md").read_bytes():
            raise RuntimeError("packaged skill copies differ")
        if not run.command("guard", [PYTHON, str(FORMAL / "check_guard.py")]):
            raise RuntimeError("guard failed; review model before checking changed code")
        run.command("versions-java", [os.environ["FV_JAVA"], "-version"])
        run.command("versions-python", [PYTHON, "-c",
                    "import sys, websockets; print(sys.version); print('websockets', websockets.__version__)"])
        run.tlc("baseline-index-loss", FORMAL / "Crash.tla", FORMAL / "Crash.cfg")
        run.tlc("baseline-no-loss-liveness", FORMAL / "Crash.tla", FORMAL / "CrashLiveness.cfg")
        run.tlc("baseline-durable-outbox", FORMAL / "Crash.tla", FORMAL / "CrashDurable.cfg")
        run.tlc("baseline-five-retries", FORMAL / "Crash.tla", FORMAL / "CrashRetry.cfg")
        run.tlc("http-physical-duplicate-limit", FORMAL / "Crash.tla", FORMAL / "CrashDuplicate.cfg", 12,
                "Invariant AtMostOneReceipt is violated")
        run.tlc("fixed-rotation-history", FORMAL / "Dedupe.tla", FORMAL / "Dedupe.cfg")
        run.tlc("fixed-receiver-idempotency", FORMAL / "Crash.tla", FORMAL / "CrashIdempotent.cfg")
        run.tlc("baseline-retained-record", FORMAL / "Dedupe.tla", FORMAL / "DedupeRetained.cfg")
        run.tlc("baseline-retained-history", FORMAL / "Dedupe.tla", FORMAL / "DedupeHistory.cfg")
        run.command("baseline-product-transition", [PYTHON, str(FORMAL / "check_transition.py")])
        run.command("baseline-product-crashes", [PYTHON, str(FORMAL / "check_crashes.py")])
        run.command("baseline-product-dedupe", [PYTHON, str(FORMAL / "check_dedupe.py")])
        run.mutation("seed-batch-index",
                     '    return seeding, changed, seen\n',
                     '    state["_files"] = seen  # FORMAL SEED: publish uncommitted observations\n    return seeding, changed, seen\n',
                     FORMAL / "Crash.tla", FORMAL / "Crash.cfg",
                     "BatchIndex = FALSE", "BatchIndex = TRUE", "NoStrandedCompletion",
                     FORMAL / "check_crashes.py", ["--scenario", "index-loss"],
                     "index recovery invariant failed")
        run.mutation("seed-unstable-id",
                     '            "Idempotency-Key": payload["event_id"],\n',
                     '            "Idempotency-Key": str(time.time_ns()),  # FORMAL SEED: new key per deliver\n',
                     FORMAL / "Crash.tla", FORMAL / "CrashIdempotent.cfg",
                     "StableIds = TRUE", "StableIds = FALSE", "StableEventIdentity",
                     FORMAL / "check_crashes.py", ["--scenario", "delivery-duplicate"],
                     "event identity invariant failed")
        run.mutation("seed-capped-history",
                     '    state["_local_completed"] = recent\n',
                     '    state["_local_completed"] = recent[-1:]  # FORMAL SEED: evict content identities\n',
                     FORMAL / "Dedupe.tla", FORMAL / "Dedupe.cfg",
                     "BoundHistory = FALSE", "BoundHistory = TRUE", "NoDuplicateEnqueue",
                     FORMAL / "check_dedupe.py", ["--scenario", "rotation"],
                     "duplicate completion enqueue")
        run.mutation("seed-drop-outbox",
                     '        candidate["_outbox"].append(queued)\n',
                     '        pass  # FORMAL SEED: discard attention event before save_state\n',
                     FORMAL / "Crash.tla", FORMAL / "CrashRetry.cfg",
                     "DropQueue = FALSE", "DropQueue = TRUE", "DurableOutbox",
                     FORMAL / "check_crashes.py", ["--scenario", "durable-outbox"],
                     "outbox invariant failed")
        run.mutation("seed-bypass-dedupe",
                     '            if already_completed:\n',
                     '            if False:  # FORMAL SEED: bypass completed-turn suppression\n',
                     FORMAL / "Dedupe.tla", FORMAL / "DedupeHistory.cfg",
                     "RespectDedupe = TRUE", "RespectDedupe = FALSE", "NoDuplicateEnqueue",
                     FORMAL / "check_dedupe.py", ["--scenario", "ordinary"],
                     "duplicate completion enqueue")
        run.command("existing-tests", [PYTHON, "-m", "unittest", "discover", "-s", "tests", "-q"])
        run.command("final-guard", [PYTHON, str(FORMAL / "check_guard.py")])
        if digest(ROOT / "t3_done_notify.py") != original_hash:
            raise RuntimeError("checkout product changed during verification")
    except Exception as exc:
        run.problems.append(type(exc).__name__ + ": " + str(exc))
        print(f"FAILED: {exc}", flush=True)
    elapsed = time.monotonic() - started
    summary = {"status": "unexpected failure" if run.problems else "fixed properties and mutation checks passed",
               "total_wall_seconds": round(elapsed, 6), "problems": run.problems,
               "checks": run.rows}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if run.problems:
        print(f"FAILED: {len(run.problems)} unexpected results; see {output}", flush=True)
        return 1
    print("Fixed no-loss, persistent completion dedupe, and receiver idempotency checks passed.", flush=True)
    print(f"All recorded outcomes and mutation/revert checks reproduced in {elapsed:.3f}s.", flush=True)
    print("Physical HTTP receipts can repeat; the unchanged AtMostOneReceipt check intentionally fails.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
