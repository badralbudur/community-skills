#!/usr/bin/env bash
# Provision a Fulcra Workspace team for this control harness, from only
# this directory's own contents -- no old inboxes, no local cache
# assumed. Thin wrapper around coordinator/bootstrap.py; see that
# script's docstring and RUNBOOK.md for the full contract.
#
# Usage: ./bootstrap.sh <team-name> [--deliverable <path>] [--dry-run]
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: ./bootstrap.sh <team-name> [--deliverable <path>] [--dry-run]" >&2
  exit 2
fi

TEAM="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v fulcra >/dev/null 2>&1 && ! command -v fulcra-api >/dev/null 2>&1; then
  echo "ERROR: neither 'fulcra' nor 'fulcra-api' CLI found on PATH." >&2
  echo "Install with: pip install fulcra-api" >&2
  exit 1
fi

DELIVERABLE_ARG=()
if [[ "$*" != *"--deliverable"* ]]; then
  DELIVERABLE_ARG=(--deliverable "$SCRIPT_DIR/../<fill-in-deliverable-path>")
  echo "WARNING: no --deliverable given; pass one explicitly for a real run." >&2
fi

python3 "$SCRIPT_DIR/coordinator/bootstrap.py" --team "$TEAM" "${DELIVERABLE_ARG[@]}" "$@"
