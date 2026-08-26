#!/usr/bin/env python3
"""Scaffold a portable outer control harness beside a rapid-prototype project.

Copies only generic templates. It does not overwrite an existing control
harness, create provider credentials, or modify deliverable source code.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = SKILL_DIR / "control_harness_templates"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target = args.output_dir.resolve()
    if target.exists() and any(target.iterdir()):
        print(f"Refusing to overwrite non-empty control harness: {target}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"Would copy {TEMPLATES} -> {target}")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    for source in TEMPLATES.rglob("*"):
        if source.is_dir():
            continue
        relative = source.relative_to(TEMPLATES)
        destination = target / relative.name.replace(".template", "") if relative.parent == Path(".") else target / relative.parent / relative.name.replace(".template", "")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text().replace("{{PROJECT_NAME}}", args.project_name))
        # Shell scripts and the coordinator's Python entry points are meant
        # to be run directly (./bootstrap.sh, ./doctor.sh, per RUNBOOK.md)
        # -- make sure that actually works out of the box rather than
        # requiring a manual chmod +x the first time someone tries it.
        if destination.suffix in (".sh", ".py"):
            destination.chmod(destination.stat().st_mode | 0o111)
    print(f"Scaffolded portable control harness: {target}")
    print("Next: fill spec.md/milestones.md from approved Grill-Me artifacts, then configure provider adapter commands in README.md, then run ./doctor.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
