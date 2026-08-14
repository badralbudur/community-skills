"""Generic task correctness contracts shared with current Coord."""

from __future__ import annotations

import pytest

from coord_engine import cli, model, okf, tasks
from coord_engine_test_helpers import FakeTransport

STAMP = "2026-07-28T21:00:00Z"


def _doc(status: str = "waiting") -> str:
    return (
        "---\n"
        "type: Task\n"
        "title: Historical 1.6.10 task\n"
        f"status: {status}\n"
        "priority: P2\n"
        "id: historical-task\n"
        "---\n\n"
        "body\n"
    )


@pytest.mark.parametrize("status,label", [
    ("done", "evidence"),
    ("abandoned", "reason"),
])
def test_born_terminal_task_requires_evidence(status, label):
    with pytest.raises(tasks.TaskError, match=label):
        tasks.new_task_doc("T", now=STAMP, status=status)

    _, content = tasks.new_task_doc(
        "T", now=STAMP, status=status, evidence="terminal event")
    assert okf.parse_frontmatter(content)["status"] == status
    assert f"{label}: terminal event" in content


def test_supersede_closes_from_any_live_state():
    for status in ("proposed", "active", "waiting", "blocked"):
        out = tasks.apply_update(
            _doc(status), now=STAMP, status="done",
            superseded_by="new-slug", evidence="superseded by new-slug")
        fm = okf.parse_frontmatter(out)
        assert fm["status"] == "done"
        assert fm["superseded_by"] == "new-slug"


def test_plain_done_still_respects_the_status_machine():
    with pytest.raises(tasks.TaskError):
        tasks.apply_update(_doc("waiting"), now=STAMP, status="done", evidence="e")


def test_unlock_field_written():
    out = tasks.apply_update(
        _doc("active"), now=STAMP, status="blocked",
        blocked_on="ash", unlock="merge PR 999")
    fm = okf.parse_frontmatter(out)
    assert fm["blocked_on"] == "ash"
    assert fm["unlock"] == "merge PR 999"


def test_on_user_types_blocked_on_field(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Ship it", "--status", "active"], transport=t)
    capsys.readouterr()
    rc = cli.main([
        "task", "block", "r", "ship-it", "--on-user", "ash",
        "--unlock", "Ash supplies the launch decision",
    ], transport=t)
    assert rc == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/ship-it.md"])
    assert fm["blocked_on"] == "user:ash"
    assert fm["unlock"] == "Ash supplies the launch decision"
    assert fm["status"] == "blocked"
    assert "needs:human" in (fm.get("tags") or [])


def test_block_requires_an_unlock_condition(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Ship it", "--status", "active"], transport=t)
    capsys.readouterr()
    assert cli.main([
        "task", "block", "r", "ship-it", "--blocked-on", "bob",
    ], transport=t) == 1
    assert "--unlock" in capsys.readouterr().err


def test_supersede_command_records_replacement(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Old task", "--status", "waiting"], transport=t)
    capsys.readouterr()
    assert cli.main([
        "task", "supersede", "r", "old-task", "--by", "new-task",
    ], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/old-task.md"])
    assert fm["status"] == "done"
    assert fm["superseded_by"] == "new-task"


def test_historical_1_6_10_task_remains_readable():
    row = model.row_from_frontmatter(
        okf.parse_frontmatter(_doc("active")),
        name="historical-task", path="team/r/task/historical-task.md")
    assert row["status"] == "active"
    assert row["blocked_on"] is None


def test_historical_terminal_task_cannot_be_rewritten():
    with pytest.raises(tasks.TaskError):
        tasks.apply_update(
            _doc("done"), now=STAMP, status="done",
            superseded_by="new-task", evidence="superseded")


def test_historical_live_task_gains_fields_only_through_legal_command(capsys):
    t = FakeTransport()
    t.put("team/r/task/historical-task.md", _doc("active"))
    assert "unlock" not in okf.parse_frontmatter(t.store["team/r/task/historical-task.md"])

    rc = cli.main([
        "task", "block", "r", "historical-task", "--blocked-on", "bob",
        "--unlock", "Bob completes the dependency",
    ], transport=t)
    assert rc == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/historical-task.md"])
    assert fm["unlock"] == "Bob completes the dependency"
    assert fm["blocked_on"] == "bob"
