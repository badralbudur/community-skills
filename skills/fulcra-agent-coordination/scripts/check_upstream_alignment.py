#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def _modules(path: Path) -> set[str]:
    return {item.stem for item in path.glob("*.py") if item.is_file()}


def _upstream_engine(path: Path) -> Path:
    candidate = path / "packages" / "coord-engine" / "coord_engine"
    return candidate if candidate.is_dir() else path


def _git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def check_alignment(
    skill_root: Path,
    upstream_root: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    vendored = _modules(skill_root / "scripts" / "coord_engine")
    upstream = _modules(_upstream_engine(upstream_root))
    included = set(manifest.get("included_modules") or [])
    excluded_raw = manifest.get("excluded_modules") or {}
    excluded = set(excluded_raw) if isinstance(excluded_raw, dict) else set()
    expected_commit = (manifest.get("comparison_upstream") or {}).get("commit")
    actual_commit = _git_head(upstream_root)

    result = {
        "actual_upstream_commit": actual_commit,
        "classified_missing_upstream": sorted((included | excluded) - upstream),
        "expected_upstream_commit": expected_commit,
        "included_missing_vendored": sorted(included - vendored),
        "upstream_commit_mismatch": bool(
            actual_commit is not None and actual_commit != expected_commit
        ),
        "upstream_unclassified": sorted(upstream - included - excluded),
        "vendored_unlisted": sorted(vendored - included),
    }
    drift_keys = (
        "classified_missing_upstream",
        "included_missing_vendored",
        "upstream_unclassified",
        "vendored_unlisted",
    )
    result["status"] = "drift" if (
        result["upstream_commit_mismatch"]
        or any(result[key] for key in drift_keys)
    ) else "aligned"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the public Coord selection manifest with an upstream checkout."
    )
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "upstream-selection.json",
    )
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "unknown", "error": str(exc)}, sort_keys=True))
        return 2
    result = check_alignment(args.manifest.parent, args.upstream, manifest)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "aligned" else 1


if __name__ == "__main__":
    raise SystemExit(main())

