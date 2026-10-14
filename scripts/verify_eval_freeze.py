"""Verify the approved eval documents have not changed since freeze."""

import hashlib
import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    checksums = json.loads((root / "evals" / "checksums.json").read_text(encoding="utf-8"))
    failures = []
    for name, expected in checksums["files"].items():
        path = root / "evals" / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            failures.append(name)
    if failures:
        print("Eval freeze mismatch: " + ", ".join(failures))
        return 1
    print(f"Verified {len(checksums['files'])} frozen eval files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
