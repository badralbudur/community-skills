"""The edition, as a browser gets it.

M5 renders an edition to Markdown and says, in as many words, that an HTML
template living there would be a hosting decision taken in the wrong milestone.
This is that milestone, so this is where the template lives.

It renders from the **structured edition**, not from the Markdown. Going
payload -> HTML rather than payload -> Markdown -> HTML matters for three
reasons and only one of them is tidiness:

* every block kind is handled explicitly and an unknown one raises, exactly as
  :mod:`newspaper.render` does -- a new department cannot quietly render as a
  paragraph of literal asterisks;
* nothing has to parse anything, and a Markdown parser is a place where an
  export a mayor wrote could turn into markup;
* every string that reaches the page goes through :func:`html.escape` at the
  leaf, so an export containing ``<script>`` is text on the page rather than a
  thing that happens to a reader.

The chrome -- what the archive calls itself, what it says about privacy, the
navigation labels -- comes from ``content/newspaper.json``'s ``site`` block,
for the reason the rest of the paper's words do: which words the paper uses is
a writing decision, and a writing decision in Python is one nobody can revise
without a programmer.

Nothing here emits a link to another origin. Not a font, not an analytics tag,
not an icon. A private URL is one ``Referer`` header away from being a public
one, so the pages are self-contained and :mod:`hosting.guard` fails the build if
that ever stops being true.
"""

import re
from html import escape

#: The paper's copy uses two inline marks and no others: ``**PUBLIC NOTICE**``
#: for a lede in small caps and ``*a quoted brief*`` for emphasis. That is a
#: convention of the *payload*, not of Markdown -- ``newspaper.render`` happens
#: to pass them through because Markdown already means that by them, and a page
#: that printed them as asterisks would be a page rendering the convention
#: rather than obeying it.
#:
#: Applied strictly *after* :func:`html.escape`, so the only tags that can come
#: out of it are the two written here. An export a mayor wrote containing an
#: asterisk gets the same treatment it gets in the Markdown edition, which is
#: the point: both renderings of one payload should say the same thing.
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_EM = re.compile(r"(?<!\*)\*(?=[^\s*])([^*]+?)(?<=\S)\*(?!\*)", re.DOTALL)


def inline(text):
    """Escape, then apply the copy's two inline marks."""
    marked = _STRONG.sub(r"<strong>\1</strong>", escape(text))
    return _EM.sub(r"<em>\1</em>", marked)


#: Block heading levels to tags. A department's own title is an ``h2``, so a
#: level-3 heading inside it is an ``h3`` -- the mapping is the same one
#: :mod:`newspaper.render` uses for ``#`` characters.
_HEADING_TAGS = {1: "h1", 2: "h2", 3: "h3", 4: "h4"}

DOCTYPE = "<!DOCTYPE html>"


def block_to_html(block):
    """One typed block from an edition payload. Mirrors ``block_to_markdown``."""
    kind = block["kind"]
    if kind == "heading":
        tag = _HEADING_TAGS.get(block.get("level", 2), "h2")
        return "<%s>%s</%s>" % (tag, inline(block["text"]), tag)
    if kind == "standfirst":
        return '<p class="standfirst">%s</p>' % inline(block["text"])
    if kind == "para":
        return "<p>%s</p>" % inline(block["text"])
    if kind == "quote":
        lines = block["text"].splitlines() or [""]
        return "<blockquote>%s</blockquote>" % "".join(
            "<p>%s</p>" % inline(line) for line in lines
        )
    if kind in ("aside", "note"):
        # Same on the page, different in kind -- an aside is an editorial joke
        # that config can switch off, a note is a factual footnote that always
        # prints. The class keeps that distinction available to a stylesheet
        # even though today it styles them alike.
        return '<p class="%s">%s</p>' % (kind, inline(block["text"]))
    if kind == "figure":
        # An illustration belonging to one article rather than to the edition --
        # today, a city's portrait in the last edition (spec #32). No width or
        # height, for the same reason the masthead image carries none: the
        # payload does not know them and config's would be a guess.
        return (
            '<figure class="city-portrait">\n<img src="%s" alt="%s">\n'
            "<figcaption>%s</figcaption>\n</figure>"
            % (escape(block["image"]), escape(block["alt"]), inline(block["caption"]))
        )
    if kind == "list":
        return "<ul>%s</ul>" % "".join("<li>%s</li>" % inline(item) for item in block["items"])
    if kind == "table":
        head = "".join("<th>%s</th>" % inline(str(column)) for column in block["columns"])
        body = "".join(
            "<tr>%s</tr>" % "".join("<td>%s</td>" % inline(str(cell)) for cell in row)
            for row in block["rows"]
        )
        return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)
    raise ValueError("no HTML renderer for block kind %r" % kind)


def department_to_html(department):
    blocks = "\n".join(block_to_html(block) for block in department["blocks"])
    return '<section class="department" id="%s">\n<h2>%s</h2>\n%s\n</section>' % (
        escape(str(department["id"])), escape(department["title"]), blocks,
    )


#: The last edition's permanent name. Not ``round-NN.html``: the final edition
#: is published in the same round as that round's own edition and is a different
#: document, so sharing a name would make one of them overwrite the other --
#: exactly what spec #27 forbids.
FINAL_PAGE_NAME = "final.html"


def edition_page_name(round_index):
    return "round-%02d.html" % round_index


def page_name_for(edition):
    """The permanent name of any edition, round or final (spec #27, #31)."""
    if edition.get("endgame"):
        return FINAL_PAGE_NAME
    return edition_page_name(edition["round"])


def _head(title, site, privacy, stylesheet=None):
    """The parts of every page that are about privacy rather than about news.

    ``noindex`` appears here *and* in ``robots.txt`` *and* in the ``X-Robots-Tag``
    header. Three copies of one instruction, because a crawler that ignores one
    of them is a normal crawler and spec #26's requirement is that the paper not
    be publicly discoverable, not that it have asked politely once.

    ``stylesheet`` is a filename or ``None``. It is a parameter rather than a
    constant because ``hosting.publish`` decides whether the stylesheet is
    published at all, and a page that linked one that was not published would be
    a page asking for a file that is not there.
    """
    lines = [
        DOCTYPE,
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="%s">' % escape(privacy["meta_robots"]),
        '<meta name="referrer" content="%s">' % escape(privacy["referrer_policy"]),
        '<meta http-equiv="Content-Security-Policy" content="%s">'
        % escape(privacy["content_security_policy"]),
        "<title>%s</title>" % escape(title),
    ]
    if stylesheet:
        lines.append('<link rel="stylesheet" href="%s">' % escape(stylesheet))
    lines.append("</head>")
    return "\n".join(lines)


def _footer(site):
    return (
        '<footer class="colophon">\n'
        "<p>%s</p>\n<p>%s</p>\n<p class=\"privacy\">%s</p>\n</footer>"
        % (
            escape(site["colophon"]),
            escape(site["identity_notice"]),
            escape(site["privacy_notice"]),
        )
    )


def _nav(links):
    """Relative links only. Every one stays inside the private address."""
    items = "".join(
        '<a href="%s">%s</a>' % (escape(href), escape(label)) for label, href in links
    )
    return '<nav class="issue-nav">%s</nav>' % items if items else ""


def edition_page(edition, site, privacy, previous_round=None, next_round=None,
                 stylesheet=None, with_image=True, final_page=False):
    """One edition, complete, at its own permanent name.

    ``final_page`` links the last edition (spec #31) from a round edition's nav.
    It is a flag rather than another ``*_round`` argument because the final
    edition has no round of its own to name -- it shares the last one's -- and
    threading a sentinel round number through here would be a lie the nav would
    then have to decode.
    """
    title = "%s — %s" % (edition["publication"], edition["edition_line"])
    links = [(site["nav"]["archive"], "index.html")]
    if previous_round is not None:
        links.append((site["nav"]["previous"], edition_page_name(previous_round)))
    if next_round is not None:
        links.append((site["nav"]["next"], edition_page_name(next_round)))
    if final_page and not edition.get("endgame"):
        links.append((site["nav"]["endgame"], FINAL_PAGE_NAME))

    parts = [
        _head(title, site, privacy, stylesheet),
        "<body>",
        '<article class="edition">',
        '<header class="masthead">',
        "<h1>%s</h1>" % escape(edition["publication"]),
        '<p class="motto">%s</p>' % escape(edition["motto"]),
        '<p class="dateline"><strong>%s</strong> · %s · %s</p>'
        % (
            escape(edition["edition_line"]),
            escape(edition["dateline"]),
            escape(edition["price_line"]),
        ),
        '<p class="weather">%s</p>' % escape(edition["weather_line"]),
        '<p class="standing">%s</p>' % escape(edition["standing_line"]),
        "</header>",
    ]

    image = edition.get("image") or {}
    if with_image and image.get("filename"):
        # No width/height attributes: the edition payload does not carry the
        # image's dimensions (a raster provider's need not match the configured
        # ones), and attributes guessed from config would be wrong exactly when
        # a provider is doing something interesting.
        parts.append(
            '<figure class="edition-image">\n'
            '<img src="%s" alt="%s">\n'
            "<figcaption>%s</figcaption>\n</figure>"
            % (
                escape(image["filename"]),
                escape(image["alt"]),
                escape(image["cutline"]),
            )
        )

    parts.extend(department_to_html(department) for department in edition["departments"])
    if edition.get("endgame"):
        # No deadline on the last page: there is no notice open and no window
        # closing, and printing one would be the paper inviting offers it has
        # just spent three articles closing the books on (spec #31).
        parts.append('<p class="issue-foot">%s</p>' % escape(edition["foot_line"]))
    else:
        parts.append(
            '<p class="issue-foot">%s. %s %s. Offers for the current notice close %s.</p>'
            % (
                escape(edition["publication"]),
                escape(site["labels"]["round"]),
                escape(str(edition["round"])),
                escape(edition["closes"]),
            )
        )
    parts.append("</article>")
    parts.append(_nav(links))
    parts.append(_footer(site))
    parts.extend(["</body>", "</html>", ""])
    return "\n".join(parts)


def archive_page(archive, entries, site, privacy, stylesheet=None, with_images=True):
    """The one URL every mayor holds (spec #26) and the whole shelf behind it (#27).

    ``entries`` are ``(round, edition)`` pairs in the order they should be
    listed; the order is ``hosting.archive_order``'s business, not this
    function's.
    """
    rows = []
    for edition in entries:
        image = edition.get("image") or {}
        picture = (
            ' <a class="picture" href="%s">%s</a>'
            % (escape(image["filename"]), escape(site["labels"]["image"]))
            if with_images and image.get("filename")
            else ""
        )
        portraits = ""
        if with_images and edition.get("endgame"):
            portraits = "".join(
                ' <a class="portrait" href="%s">%s</a>'
                % (escape(entry["filename"]), escape(entry["city"]))
                for entry in edition.get("city_images") or ()
                if entry.get("filename")
            )
        rows.append(
            '<li%s><a class="issue" href="%s">%s</a> <span class="when">%s</span>%s%s</li>'
            % (
                ' class="final"' if edition.get("endgame") else "",
                escape(page_name_for(edition)),
                escape(edition["edition_line"]),
                escape(edition["dateline"]),
                picture,
                portraits,
            )
        )

    body = (
        "<ul class=\"issues\">\n%s\n</ul>" % "\n".join(rows)
        if rows
        else '<p class="empty">%s</p>' % escape(site["empty_archive"])
    )

    return "\n".join(
        [
            _head(site["archive_title"], site, privacy, stylesheet),
            "<body>",
            '<header class="masthead">',
            "<h1>%s</h1>" % escape(archive["publication"]),
            '<p class="motto">%s</p>' % escape(archive["motto"]),
            "</header>",
            '<section class="archive">',
            "<h2>%s</h2>" % escape(site["archive_heading"]),
            '<p class="standfirst">%s</p>' % escape(site["archive_blurb"]),
            body,
            '<p class="count">%d %s</p>' % (len(entries), escape(site["labels"]["editions_count"])),
            "</section>",
            _footer(site),
            "</body>",
            "</html>",
            "",
        ]
    )


def robots_txt(site, privacy):
    """``Disallow: /``, with the paper's own explanation above it.

    The preamble is content because it is the paper talking; the two directives
    under it are mechanical and stay here. ``robots.txt`` is served without the
    address in front of it -- it is the exclusion notice, it contains no secret,
    and a crawler that has somehow found the host should be able to read it.
    """
    lines = list(site["robots_preamble"])
    lines.extend(["", "User-agent: *", "Disallow: /"])
    if privacy.get("meta_robots"):
        lines.append("# Every page also carries: %s" % privacy["meta_robots"])
    return "\n".join(lines) + "\n"
