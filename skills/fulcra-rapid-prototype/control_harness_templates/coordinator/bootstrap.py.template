#!/usr/bin/env python3
"""Bootstrap a Fulcra Workspace team for this control harness.

Provisions the OKF-compliant team directory structure (per the
fulcra-workspaces skill: https://github.com/fulcradynamics/agent-skills)
in the user's Fulcra file store, using only this control harness's own
local contents -- no old inboxes, no local cache, no assumption of prior
state. This is what makes "resume on a fresh machine" work: re-running
this against the same team name reconnects to existing Workspace state
rather than requiring anything carried over locally.

Uses the `fulcra-api` CLI directly (`fulcra file upload` / `fulcra file
list`) rather than a bundled SDK dependency, matching this skill's own
pattern of using `fulcra-api`/`fulcra` as the CLI entrypoint.

Usage:
    python coordinator/bootstrap.py --team <team-name> --deliverable <path>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

CONTROL_HARNESS_ROOT = Path(__file__).resolve().parent.parent


def _fulcra_file_exists(remote_path: str) -> bool:
    """Whether a file already exists at `remote_path` in the user's Fulcra
    file store. Used to decide create-vs-join for the team, per
    fulcra-workspaces' explicit "check before creating" requirement."""
    result = subprocess.run(
        ["fulcra", "file", "stat", remote_path],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _upload_text(remote_path: str, content: str, dry_run: bool) -> None:
    print(f"  {'[dry-run] would upload' if dry_run else 'uploading'}: {remote_path}")
    if dry_run:
        return
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(content)
        local_path = f.name
    try:
        result = subprocess.run(
            ["fulcra", "file", "upload", local_path, remote_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    FAILED: {result.stderr.strip()}", file=sys.stderr)
            raise SystemExit(1)
    finally:
        Path(local_path).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True, help="Fulcra Workspace team name")
    parser.add_argument(
        "--deliverable",
        required=True,
        type=Path,
        help="Path to the deliverable project repo this harness governs",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    team_prefix = f"team/{args.team}"
    role_path = f"{team_prefix}/role.md"

    if _fulcra_file_exists(role_path):
        print(
            f"Team '{args.team}' already exists ({role_path} found) -- "
            f"joining existing Workspace rather than recreating it."
        )
    else:
        print(f"Provisioning new Workspace team: {args.team}")
        _upload_text(
            role_path,
            f"---\nteam: {args.team}\n---\n\n"
            f"# {args.team}\n\n"
            f"Control harness team for the deliverable at "
            f"`{args.deliverable}`. Governs milestone execution, "
            f"independent Generator/Evaluator evaluation, and durable "
            f"decision/escalation tracking. See this control harness's "
            f"own `spec.md`/`RUNBOOK.md` for the full contract.\n",
            args.dry_run,
        )
        _upload_text(
            f"{team_prefix}/progress.md",
            f"---\nteam: {args.team}\n---\n\n"
            f"# Progress\n\nNo milestones run yet.\n",
            args.dry_run,
        )
        _upload_text(
            f"{team_prefix}/completed.md",
            f"---\nteam: {args.team}\n---\n\n"
            f"# Completed\n\nNo milestones completed yet.\n",
            args.dry_run,
        )
        _upload_text(
            f"{team_prefix}/log.md",
            f"---\nteam: {args.team}\n---\n\n"
            f"# Log\n\n- Team created by control-harness bootstrap.\n",
            args.dry_run,
        )

    print()
    print("Required Fulcra Workspace paths for this team:")
    print(f"  {team_prefix}/role.md")
    print(f"  {team_prefix}/progress.md")
    print(f"  {team_prefix}/completed.md")
    print(f"  {team_prefix}/log.md")
    print(f"  {team_prefix}/knowledge/           (earned findings)")
    print(f"  {team_prefix}/decision/            (pending decision requests)")
    print(f"  {team_prefix}/session/             (session summaries)")
    print(f"  {team_prefix}/member/generator/inbox/")
    print(f"  {team_prefix}/member/evaluator/inbox/")
    print()
    print(
        "An integration adapter (or you, manually) should upload this "
        "control harness's static artifacts (spec.md, decisions.md, "
        "coordinator/milestones.md, etc.) to durable Workspace paths "
        "before the first role run, per this skill's Workspace contract."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
