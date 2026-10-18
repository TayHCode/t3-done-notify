#!/usr/bin/env python3
"""Fail when t3_done_notify.py changes without a reviewed model hash update."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, default=ROOT / "t3_done_notify.py")
    parser.add_argument("--recorded", type=Path, default=ROOT / "formal/guarded.json")
    args = parser.parse_args()
    expected = json.loads(args.recorded.read_text())["sha256"]["t3_done_notify.py"]
    actual = hashlib.sha256(args.product.read_bytes()).hexdigest()
    if actual != expected:
        print("FAIL: t3_done_notify.py changed since the model was reviewed.")
        print("Follow .claude/skills/verify-watcher/SKILL.md; review/mirror the code")
        print("change before recording a new hash in formal/guarded.json.")
        print(f"Recorded: {expected}\nActual:   {actual}")
        return 1
    print(f"PASS: guarded product hash {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
