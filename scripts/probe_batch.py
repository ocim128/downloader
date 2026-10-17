#!/usr/bin/env python3
"""Ad-hoc batch prober for candidate ids (spike only, never from pytest)."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_candidates import classify


def main() -> int:
    cands = list(sys.argv[1:])
    if not cands:
        print(__doc__)
        return 2
    rows = []
    for c in cands:
        try:
            r = classify(c)
        except Exception as e:
            r = {"id": c, "status": -1, "type": f"exc {type(e).__name__}", "detail": str(e)[:60]}
        print(f"{r['id']:<20} {r['status']} {r['type']:<16} {r['detail'][:70]}")
        rows.append(r)
        time.sleep(4)
    Path(".tmp_probe.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
