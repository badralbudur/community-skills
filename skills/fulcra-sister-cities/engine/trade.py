"""What a city is allowed to order, and what it is not (spec #13a).

Spec #13a, from the user decision of 2026-08-31, draws one line through the
import needs: a need "describes actual tradable imports -- e.g. food or candy,
materials, equipment, living things, cultural works, or specialist services",
and "may not reduce to a request for generic advice or civic problem solving".

That line has to be enforced in three places or it is enforced nowhere:

* the **seeded list** (``content/import_needs.json``), checked when a game
  loads its content, so a seed that drifted back into advice refuses to start a
  game rather than turning up as round 7's notice;
* a **player-suggested** addition to the pool (spec #13, #33);
* an importing mayor's **freeform request** (spec #13), which is the door with
  a human on the other side of it and therefore the one that needs the clearest
  refusal message.

:class:`TradePolicy` is that one check, and its vocabulary is *content* rather
than code: the families, the supply verbs and the advice markers all live in
``content/import_needs.json``'s ``trade_policy`` block, because which phrasings
read as "send us a crate" and which read as "tell us what to do" is a writing
judgement, and writing judgements belong in the content file with the writing.

The check is deliberately blunt in one direction and forgiving in the other. It
refuses on an explicit marker ("what should", "ideas for", "solve"), and it
requires an affirmative signal (a supply verb in the exporter prompt, a declared
trade family). It does not attempt to decide whether "a rivalry, in kind" is
really a tradable good -- that is a judgement for the Evaluator's #33 review and
for the mayors, who will vote with their crates.
"""

import re

from .errors import ContentError, TradeRefused

#: Fields a need must carry whatever door it came through. ``exporter_prompt``
#: and ``excess_flavor`` are filled from the freeform defaults when a mayor does
#: not write their own, which is why they are not required *of the mayor* -- see
#: :meth:`TradePolicy.freeform_need`.
REQUIRED_NEED_FIELDS = ("id", "category", "trade_family", "title", "need_brief",
                        "exporter_prompt")


class TradePolicy:
    """``content/import_needs.json``'s ``trade_policy`` block, as a check."""

    def __init__(self, doc):
        if not isinstance(doc, dict) or not doc:
            raise ContentError(
                "the import-need file has no trade_policy block; spec #13a's rule "
                "about what may be ordered is content, and the engine will not "
                "invent one"
            )
        #: The block as written, so a hand-made content fixture (a test's tiny
        #: pool, say) can borrow the real policy rather than restate it and
        #: quietly drift away from the rule it is meant to be obeying.
        self.doc = dict(doc)
        self.families = dict(doc.get("families") or {})
        self.rules = list(doc.get("rules") or [])
        self.supply_verbs = [v.lower() for v in (doc.get("supply_verbs") or [])]
        self.advice_markers = [m.lower() for m in (doc.get("advice_markers") or [])]
        self.freeform = dict(doc.get("freeform") or {})
        if not self.families:
            raise ContentError("trade_policy.families is empty (spec #13a)")
        if not self.supply_verbs:
            raise ContentError("trade_policy.supply_verbs is empty (spec #13a)")
        if not self.advice_markers:
            raise ContentError("trade_policy.advice_markers is empty (spec #13a)")
        self._verb_re = re.compile(
            r"\b(?:%s)\b" % "|".join(re.escape(verb) for verb in self.supply_verbs),
            re.IGNORECASE,
        )

    # -- the check --------------------------------------------------------

    def check_need(self, need, where=None):
        """Refuse a need that is not an order for goods or services.

        Returns the need unchanged so this can be used inline. Raises
        :class:`~engine.errors.TradeRefused` with the offending phrase.
        """
        where = where or need.get("id") or "an import need"
        for field in REQUIRED_NEED_FIELDS:
            if not need.get(field):
                raise TradeRefused(
                    "%s is missing %r; every import need names a category, a trade "
                    "family and what is being bought (spec #13, #13a)" % (where, field),
                    where=where,
                )

        family = need["trade_family"]
        if family not in self.families:
            raise TradeRefused(
                "%s declares trade_family %r; spec #13a's kinds of tradable thing are "
                "%s" % (where, family, sorted(self.families)),
                where=where,
                phrase=family,
            )

        marker = self.advice_marker_in(
            " ".join([need.get("title", ""), need["need_brief"], need["exporter_prompt"]])
        )
        if marker:
            raise TradeRefused(
                "%s reads as a request for advice rather than an order for goods or "
                "services -- it says %r. Spec #13a: name what the city is buying "
                "(food, materials, equipment, living things, cultural works, a "
                "specialist service) and let the other mayors decide what to put in "
                "the crate." % (where, marker),
                where=where,
                phrase=marker,
            )

        if not self._verb_re.search(need["exporter_prompt"]):
            raise TradeRefused(
                "%s's exporter prompt asks for no consignment; it must use one of %s "
                "so an exporting mayor is being asked to supply something (spec #13a, "
                "#15)" % (where, self.supply_verbs),
                where=where,
            )
        return need

    def advice_marker_in(self, text):
        """The first advice marker in ``text``, or ``None``.

        ``{city}`` in a marker stands for a city name, which is either the
        unrendered placeholder or a capitalised word -- "tell {city} what" has
        to catch both the seed as written and a mayor who typed out their own
        city. It deliberately does *not* stand for any word at all: that would
        make "fix {city}" fire on "the crew who fix it in place", which is a
        sentence about a consignment and not about advice.
        """
        text = text or ""
        for marker in self.advice_markers:
            if "{city}" not in marker:
                # Whole words: an archive whose minutes hold "the decision that
                # explains the pipes" is describing its stock, not asking for an
                # explanation, and only a word-boundary match can tell the two
                # apart.
                if re.search(r"\b%s\b" % re.escape(marker), text, re.IGNORECASE):
                    return marker
                continue
            pattern = r"\b%s" % re.escape(marker).replace(
                re.escape("{city}"), r"(?P<city>\S+)"
            )
            for found in re.finditer(pattern, text, re.IGNORECASE):
                stood_in = found.group("city")
                if stood_in.lower() == "{city}" or stood_in[:1].isupper():
                    return marker
        return None

    # -- freeform requests -------------------------------------------------

    def freeform_need(self, request, need_id, proposed_by_city=None):
        """A mayor's own order, in the same shape as a seed (spec #13).

        The mayor supplies what they are buying; this fills in the parts every
        need has -- an id, an exporter prompt, an excess flavour for the endgame
        -- from the content file's ``trade_policy.freeform`` defaults rather
        than from anything hardcoded here. The result goes through
        :meth:`check_need` like any seed, which is the whole point: a freeform
        request is a first-class import need, not a bypass.
        """
        if not isinstance(request, dict):
            raise TradeRefused(
                "a freeform import request is a mapping with %s; got %r"
                % (list(self.freeform.get("required_fields") or ()), type(request).__name__),
                where=need_id,
            )
        missing = [
            field for field in (self.freeform.get("required_fields") or ())
            if not request.get(field)
        ]
        if missing:
            raise TradeRefused(
                "this freeform import request is missing %s. %s"
                % (missing, self.freeform.get("note_to_mayor", "")),
                where=need_id,
            )
        need = {
            "id": need_id,
            "category": request["category"],
            "trade_family": request["trade_family"],
            "title": request["title"],
            "need_brief": request["need_brief"],
            "exporter_prompt": request.get("exporter_prompt")
            or self.freeform.get("default_exporter_prompt", ""),
            "excess_flavor": request.get("excess_flavor")
            or self.freeform.get("default_excess_flavor", ""),
            "tags": list(request.get("tags") or ["freeform"]),
            "source": "freeform",
        }
        if proposed_by_city:
            need["requested_by_city"] = proposed_by_city
        return self.check_need(need, where="the freeform request %s" % need_id)

    def freeform_id(self, ordinal):
        return "%s-%02d" % (self.freeform.get("id_prefix", "need-freeform"), ordinal)

    # -- reporting ---------------------------------------------------------

    def describe(self):
        """What a facilitator's agent shows a mayor who is about to order."""
        return {
            "families": {
                key: {"label": value.get("label"), "examples": list(value.get("examples") or ())}
                for key, value in self.families.items()
            },
            "rules": list(self.rules),
            "note_to_mayor": self.freeform.get("note_to_mayor"),
            "required_fields": list(self.freeform.get("required_fields") or ()),
            "spec": "#13, #13a",
        }
