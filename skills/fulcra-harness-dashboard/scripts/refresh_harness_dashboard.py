#!/usr/bin/env python3
"""Refresh curated harness dashboard data from durable Workspace downloads.

Usage:
  python refresh_harness_dashboard.py \
    --data public/data/dashboard-data.json \
    --status /tmp/status-summary.md \
    --progress /tmp/milestone-progress.md

The caller downloads files from Fulcra; this script does not need API
credentials and never copies raw workspace artifacts into public/.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def section(markdown: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)", markdown, re.M | re.S)
    return " ".join(match.group(1).strip().split()) if match else ""


def meta(markdown: str, label: str) -> str:
    match = re.search(rf"^- \*\*{re.escape(label)}:\*\*\s*(.+)$", markdown, re.M)
    return match.group(1).strip() if match else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    args = parser.parse_args()

    data = json.loads(args.data.read_text())
    status = args.status.read_text() if args.status.exists() else ""
    progress = args.progress.read_text() if args.progress.exists() else ""
    current_match = re.search(r"^current:\s*(.+)$", progress, re.M)
    completed_match = re.search(r"^completed:\s*(.*)$", progress, re.M)
    current = current_match.group(1).strip() if current_match else data["overview"].get("currentMilestone")
    completed = [x.strip() for x in (completed_match.group(1) if completed_match else "").split(",") if x.strip()]

    if current and not current.startswith("("):
        data["overview"]["currentMilestone"] = current
    if completed:
        data["overview"]["completedMilestones"] = completed
    for milestone in data.get("milestones", []):
        if milestone["id"] in completed:
            milestone["status"] = "passed"
        elif milestone["id"] == current:
            milestone["status"] = "current"
        elif milestone.get("status") == "current":
            milestone["status"] = "queued"

    outcome = meta(status, "Outcome") or "UPDATED"
    milestone = meta(status, "Milestone") or current or "unknown"
    where = section(status, "Where we are")
    going = section(status, "Where we're going")
    next_bearing = section(status, "Next bearing")
    data["checkpoint"] = {
        "headline": f"{milestone}: {outcome}",
        "whereWeAre": where,
        "whereWeGo": going,
        "nextAction": next_bearing,
    }
    data["overview"]["workspaceStatus"] = "Needs operator review" if ("ESCALATED" in outcome or "DECISION" in outcome) else "Ready"
    data["project"]["lastUpdated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    args.data.write_text(json.dumps(data, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
