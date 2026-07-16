from coord_engine import transport


def test_parse_list_output_basic():
    text = "10B     2026-01-02 03:04PM UTC  alpha.md\n20B     2026-01-02 03:05PM UTC  beta.md"
    entries = transport.parse_list_output(text)
    assert len(entries) == 2
    assert entries[0] == {
        "name": "alpha.md", "size": "10B", "mtime": "2026-01-02 03:04PM UTC", "is_dir": False,
    }
    assert entries[1]["name"] == "beta.md"


def test_parse_list_output_directory_entry():
    entries = transport.parse_list_output("0B      2026-01-02 03:04PM UTC  subdir/")
    assert entries[0]["is_dir"] is True


def test_parse_list_output_empty():
    assert transport.parse_list_output("") == []
    assert transport.parse_list_output("\n\n") == []


def test_parse_stat_output():
    # Authored input, not a capture: the version tokens are opaque to the parser
    # (verbatim passthrough), and the timestamps keep their colons because
    # ``parse_stat_output`` splits each line on the first colon only.
    text = (
        "/team/demo/notes.md (20 bytes)\n"
        "Uploaded: 2026-01-02T15:05:00Z\n"
        "Version: version-b\n"
        "Previous Versions: 1\n"
        "- version-a 2026-01-02T15:04:00Z (10 bytes)"
    )
    st = transport.parse_stat_output(text)
    assert st["uploaded"] == "2026-01-02T15:05:00Z"
    assert st["version"] == "version-b"
    assert st["previous_count"] == 1
    assert st["previous"][0]["version"] == "version-a"
    assert st["path"] == "/team/demo/notes.md"


def test_parse_stat_no_previous():
    text = "/x.md (10 bytes)\nUploaded: 2026-01-02T00:00:00Z\nVersion: abc\nPrevious Versions: 0"
    st = transport.parse_stat_output(text)
    assert st["previous_count"] == 0
    assert st["previous"] == []


def test_list_dir_sorted_by_name():
    # the real transport must return list entries sorted by name (determinism for
    # "last wins" folds). Simulate parse output order != sorted, then sort.
    entries = transport.parse_list_output(
        "1B  2026-01-02 03:04PM UTC  zzz.md\n1B  2026-01-02 03:04PM UTC  aaa.md")
    names = [e["name"] for e in sorted(entries, key=lambda e: e.get("name") or "")]
    assert names == ["aaa.md", "zzz.md"]


# --- transport.updates() (data-updates feed) ---
#
# updates() runs through the hard-bounded runner ``run_bounded`` (Popen + group
# kill), so the seam these tests patch is ``run_bounded`` — returning the
# ``(returncode, stdout, stderr)`` tuple the real one yields.

def _fake_run(rc, out, calls):
    def run(argv, timeout, **kw):
        calls.append(argv)
        return (rc, out, "")
    return run


def test_updates_parses_file_changes(monkeypatch):
    from coord_engine import transport as tr
    t = tr.FulcraFileTransport(command=["uv", "tool", "run", "fulcra-api"])
    calls = []
    monkeypatch.setattr(tr, "run_bounded",
                        _fake_run(0, '{"file_changes": [{"full_name": "/team/r/task/a.md"}]}', calls))
    got = t.updates("900 seconds")
    assert got == [{"full_name": "/team/r/task/a.md"}]
    # exact command: the transport's own base verbatim — no binary rewriting
    assert calls == [["uv", "tool", "run", "fulcra-api", "data-updates", "900 seconds"]]


def test_updates_never_raises(monkeypatch):
    from coord_engine import transport as tr
    t = tr.FulcraFileTransport(command=["fulcra-api"])
    for rc, out in ((2, ""), (0, "not json"), (0, '{"file_changes": "nope"}')):
        monkeypatch.setattr(tr, "run_bounded", _fake_run(rc, out, []))
        assert t.updates("60 seconds") is None
    def boom(argv, timeout, **kw):
        raise OSError("no binary")
    monkeypatch.setattr(tr, "run_bounded", boom)
    assert t.updates("60 seconds") is None
