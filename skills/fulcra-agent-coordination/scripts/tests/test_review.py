from coord_engine import review


def _v(reviewer, verdict):
    return {"reviewer": reviewer, "verdict": verdict}


def test_normalize_verdict():
    assert review.normalize_verdict("approve") == "approve"
    assert review.normalize_verdict("LGTM") == "approve"
    assert review.normalize_verdict("request-changes") == "changes"
    assert review.normalize_verdict("meh") is None


def test_pending_when_no_verdicts():
    assert review.tally([])["state"] == review.PENDING


def test_approved_on_single_approve():
    assert review.tally([_v("a", "approve")])["state"] == review.APPROVED


def test_changes_dominates():
    t = review.tally([_v("a", "approve"), _v("b", "changes")])
    assert t["state"] == review.CHANGES
    assert t["changes"] == ["b"]


def test_last_verdict_per_reviewer_wins():
    # reviewer flips changes -> approve
    t = review.tally([_v("a", "changes"), _v("a", "approve")])
    assert t["state"] == review.APPROVED


def test_required_reviewers_gate_approval():
    t = review.tally([_v("a", "approve")], required=["a", "b"])
    assert t["state"] == review.PENDING
    assert t["pending_required"] == ["b"]
    t2 = review.tally([_v("a", "approve"), _v("b", "approve")], required=["a", "b"])
    assert t2["state"] == review.APPROVED


def test_garbage_verdicts_ignored():
    t = review.tally([{"reviewer": "a"}, {"verdict": "approve"}, "nope", _v("b", "approve")])
    assert t["state"] == review.APPROVED
    assert t["approvals"] == ["b"]


def test_head_verdict_filename_parsing_keeps_legacy_compatibility():
    head = "a" * 40
    assert review.parse_verdict_filename("alice.md") == ("alice", None)
    assert review.parse_verdict_filename(f"{head}--alice.md", head=head) == (
        "alice", None)
    assert review.parse_verdict_filename(
        f"{head}--alice--2026-08-14T12:00:00Z-deadbeef.md", head=head,
    ) == ("alice", "2026-08-14T12:00:00Z")
    assert review.parse_verdict_filename(f"{'b' * 40}--alice.md", head=head) is None


def test_newest_append_only_verdict_wins_without_deleting_history():
    kept, folded = review.fold_newest_per_reviewer([
        {"reviewer": "alice", "name": "old", "sort_key": "2026-08-14T12:00:00Z",
         "verdict": "changes"},
        {"reviewer": "alice", "name": "new", "sort_key": "2026-08-14T12:01:00Z",
         "verdict": "approve"},
        {"reviewer": "bob", "name": "only", "sort_key": "2026-08-14T12:00:30Z",
         "verdict": "approve"},
    ])
    assert folded == 1
    assert [(row["reviewer"], row["verdict"]) for row in kept] == [
        ("alice", "approve"), ("bob", "approve")]
