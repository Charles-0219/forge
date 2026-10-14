"""Check imported runtime hashes without reading the upstream checkout or user data."""

import hashlib
import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "docs" / "baseline.json").read_text(encoding="utf-8"))
    failures = []
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid manifest path")
        if relative != Path("requirements.txt") and not (
            relative.parent == Path("agents") and relative.suffix == ".py"
        ):
            raise ValueError("Manifest entry outside runtime allowlist")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            failures.append(str(relative))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["phase1_sha256"]:
            failures.append(str(relative))
    if failures:
        print("Hash mismatch or missing file: " + ", ".join(failures))
        return 1
    print(f"Verified {len(manifest['files'])} runtime files; baseline {manifest['baseline_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
