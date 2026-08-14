import importlib.util
import hashlib
import json
import re
from pathlib import Path


SKILL = Path(__file__).resolve().parents[2]
SCRIPT = SKILL / "scripts" / "check_upstream_alignment.py"
MANIFEST = SKILL / "upstream-selection.json"


def load_checker():
    spec = importlib.util.spec_from_file_location("alignment_checker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_has_honest_snapshot_provenance_and_full_comparison_sha():
    manifest = json.loads(MANIFEST.read_text())

    assert manifest["snapshot"]["engine_version"] == "1.6.10"
    assert manifest["snapshot"]["exact_upstream_commit"] is None
    assert "not recorded" in manifest["snapshot"]["provenance"].lower()
    comparison = manifest["comparison_upstream"]
    assert comparison["repository"] == "https://github.com/ashfulcra/fulcra-tools"
    assert len(comparison["commit"]) == 40
    assert all(character in "0123456789abcdef" for character in comparison["commit"])
    assert comparison["engine_version"] == "1.11.0"


def test_manifest_lists_every_vendored_module_once():
    manifest = json.loads(MANIFEST.read_text())
    listed = manifest["included_modules"]
    vendored = sorted(
        path.stem
        for path in (SKILL / "scripts" / "coord_engine").glob("*.py")
    )

    assert len(listed) == len(set(listed))
    assert sorted(listed) == vendored
    assert not set(listed).intersection(manifest["excluded_modules"])


def test_checker_classifies_every_upstream_module_and_reports_drift(tmp_path):
    checker = load_checker()
    manifest = json.loads(MANIFEST.read_text())
    upstream = tmp_path / "packages" / "coord-engine" / "coord_engine"
    upstream.mkdir(parents=True)
    classified = set(manifest["included_modules"]) | set(manifest["excluded_modules"])
    for name in classified:
        (upstream / f"{name}.py").write_text("# fixture\n")

    aligned = checker.check_alignment(SKILL, tmp_path, manifest)
    assert aligned["status"] == "aligned"
    assert aligned["upstream_unclassified"] == []

    (upstream / "new_policy.py").write_text("# fixture\n")
    drift = checker.check_alignment(SKILL, tmp_path, manifest)
    assert drift["status"] == "drift"
    assert drift["upstream_unclassified"] == ["new_policy"]


def test_public_alignment_files_contain_no_private_topology():
    text = MANIFEST.read_text() + (SKILL / "ALIGNMENT.md").read_text()
    hashed_private_tokens = (
        (8, "0e9e316982776056e8be5ffd43ed10f8cd16a6daa74177ec265f047c70bfd4cc"),
        (18, "5ec1b025884da59658638bb249c4def570432435ce6fa44b0096dbd187d179ac"),
        (17, "08f165568f3fa937dd7cbd2b60a61534bb8629df9829b916587bbeb2e040fa2b"),
        (7, "fbca3a097c1c8b690cbcccf3a1d463558817e32d5761cd4c6b923709cefef438"),
    )
    for length, forbidden_digest in hashed_private_tokens:
        observed = {
            hashlib.sha256(text[start:start + length].encode()).hexdigest()
            for start in range(max(0, len(text) - length + 1))
        }
        assert forbidden_digest not in observed

    private_identity_shape = re.compile(
        r"\b(?:codex|claude-code|openclaw):[A-Za-z0-9._-]+:[A-Za-z0-9._-]+\b"
    )
    machine_name_shape = re.compile(
        r"\b[A-Za-z][A-Za-z0-9]*-(?:MBP|MacBook|Workstation|Desktop)"
        r"(?:-[A-Za-z0-9]+)*\b"
    )
    assert private_identity_shape.search(text) is None
    assert machine_name_shape.search(text) is None
