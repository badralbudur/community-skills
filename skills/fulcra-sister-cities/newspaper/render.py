"""Turning an edition into something a person reads.

The edition is a structured payload of typed blocks, and this module renders it
to Markdown. Keeping them separate is what makes the redaction and tone checks
in :mod:`newspaper.edition` worth anything: every printed sentence is a leaf of
the payload, so a check that walks the payload has seen the whole paper, and a
check that also reads this module's output has seen it the way a player will.

Markdown, and not HTML: this is the edition as a file, and the edition as a page
is :mod:`hosting.page`, which renders the same typed blocks for a browser. Both
render from the payload rather than one from the other, so neither has to parse
anything -- a Markdown parser standing between an export a mayor wrote and a
reader's browser is a place that turns text into markup.
"""

_HEADINGS = {1: "#", 2: "##", 3: "###", 4: "####"}

#: The last edition's stem on disk. It is not ``round-NN`` because the final
#: edition shares the last round's number with that round's own edition and is a
#: different document (spec #31); giving it the same name would be one of them
#: overwriting the other, which is the thing spec #27 exists to prevent.
FINAL_STEM = "final"


def block_to_markdown(block):
    kind = block["kind"]
    if kind == "heading":
        return "%s %s" % (_HEADINGS.get(block.get("level", 2), "##"), block["text"])
    if kind == "standfirst":
        return "*%s*" % block["text"]
    if kind == "para":
        return block["text"]
    if kind == "quote":
        return "\n".join("> %s" % line for line in block["text"].splitlines() or [""])
    if kind in ("aside", "note"):
        # An aside is an editorial joke and is dropped when config says the paper
        # is not to be funny; a note is a factual footnote and always prints.
        # They look the same on the page and are deliberately different in kind.
        return "_%s_" % block["text"]
    if kind == "figure":
        # A picture inside a department, as against the one the edition carries
        # at its masthead. The last edition's per-city portraits are the only
        # ones today (spec #32); the block is generic because "an illustration
        # belonging to this article" is not an endgame-specific idea.
        return "![%s](%s)\n\n*%s*" % (block["alt"], block["image"], block["caption"])
    if kind == "list":
        return "\n".join("- %s" % item for item in block["items"])
    if kind == "table":
        columns = block["columns"]
        lines = [
            "| %s |" % " | ".join(str(column) for column in columns),
            "| %s |" % " | ".join("---" for _ in columns),
        ]
        lines.extend(
            "| %s |" % " | ".join(str(cell) for cell in row) for row in block["rows"]
        )
        return "\n".join(lines)
    raise ValueError("no renderer for block kind %r" % kind)


def department_to_markdown(department):
    parts = ["## %s" % department["title"]]
    parts.extend(block_to_markdown(block) for block in department["blocks"])
    return "\n\n".join(parts)


def to_markdown(edition):
    """One edition, as the paper reads."""
    masthead = [
        "# %s" % edition["publication"],
        "*%s*" % edition["motto"],
        " · ".join(
            [
                "**%s**" % edition["edition_line"],
                edition["dateline"],
                edition["price_line"],
            ]
        ),
        edition["weather_line"],
        "_%s_" % edition["standing_line"],
    ]

    image = edition.get("image") or {}
    if image.get("filename"):
        masthead.append("![%s](%s)" % (image["alt"], image["filename"]))
        masthead.append("*%s*" % image["cutline"])

    parts = ["\n\n".join(masthead), "---"]
    parts.extend(department_to_markdown(department) for department in edition["departments"])
    # The last edition's foot is its own sentence, because the usual one is a
    # deadline and the final edition has no notice open and no window closing
    # (spec #31). It comes from the masthead in content/newspaper.json.
    parts.append(
        "---\n\n_%s_" % edition["foot_line"]
        if edition.get("endgame")
        else "---\n\n_%s. Round %s. Offers for the current notice close %s._"
        % (edition["publication"], edition["round"], edition["closes"])
    )
    return "\n\n".join(parts) + "\n"


def archive_index_to_markdown(archive):
    """A local index of the editions on disk (#27).

    The *served* archive index is :func:`hosting.page.archive_page`; this one is
    for reading the run in the repository, which is a different reader with a
    different need -- a Markdown file next to the Markdown editions.
    """
    lines = [
        "# %s" % archive["publication"],
        "*%s*" % archive["motto"],
        "",
        "The complete run of %s, oldest first. Every edition stays here; a new one "
        "never replaces an old one (spec #27)." % archive["game"],
        "",
    ]
    for edition in archive["editions"]:
        image = edition.get("image") or {}
        lines.append(
            "- [%s — %s](round-%02d.md)%s"
            % (
                edition["edition_line"],
                edition["dateline"],
                edition["round"],
                "" if not image.get("filename") else " · [image](%s)" % image["filename"],
            )
        )
    final = archive.get("final")
    if final:
        image = final.get("image") or {}
        lines.append(
            "- [%s — %s](%s.md)%s"
            % (
                final["edition_line"],
                final["dateline"],
                FINAL_STEM,
                "" if not image.get("filename") else " · [image](%s)" % image["filename"],
            )
        )
        portraits = [entry for entry in (final.get("city_images") or ()) if entry.get("filename")]
        if portraits:
            lines.append(
                "  - city portraits (spec #32): %s"
                % ", ".join(
                    "[%s](%s)" % (entry["city"], entry["filename"]) for entry in portraits
                )
            )
    lines.extend(
        [
            "",
            "_These are the local files. The paper itself is served by `hosting/` at "
            "an unguessable subdomain with `noindex` set, where `index.html` opens the "
            "current issue and the shelf of back issues is `archive.html` rather than "
            "this file (spec #26, #27, #30a)._",
        ]
    )
    return "\n".join(lines) + "\n"
