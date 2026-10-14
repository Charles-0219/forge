import hashlib
import json
from pathlib import Path


def test_approved_eval_files_match_freeze():
    root = Path(__file__).resolve().parents[1]
    checksums = json.loads((root / "evals" / "checksums.json").read_text(encoding="utf-8"))
    for name, expected in checksums["files"].items():
        digest = hashlib.sha256((root / "evals" / name).read_bytes()).hexdigest()
        assert digest == expected
