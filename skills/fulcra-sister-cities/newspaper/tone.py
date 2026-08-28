"""The mechanical half of spec #30.

Spec #30 asks for funny, fun and colourful, allows humour that is pointed rather
than uniformly laudatory, and forbids snide or mean. Three of those four are
judgements and are graded as judged criteria by the Evaluator role. The fourth
has a floor that can be checked by a machine, and this is it: a register of
words whose only job in a sentence is to attack somebody.

The check is deliberately modest about itself. Passing it does not mean an
edition is kind; it means the edition does not contain the specific vocabulary
that is never kind. The register lives in ``content/newspaper.json`` because
which words are out of bounds is an editorial decision, and
``config.newspaper.tone.disallow_snide_or_mean`` decides whether tripping it
blocks publication.

The other three flags are honoured elsewhere, and honoured for real rather than
echoed:

* ``allow_pointed_humor`` filters frames in :class:`newspaper.copy.Chooser`
* ``funny`` drops the editorial asides in :mod:`newspaper.departments`
* ``colorful`` selects the monochrome palette in :mod:`newspaper.svg`
"""

import re

from engine.errors import RuleViolation


class TonePolicy:
    """What this game's ``newspaper.tone`` block asks the paper to be."""

    __slots__ = ("funny", "colorful", "allow_pointed", "disallow_snide", "forbidden")

    def __init__(self, config, copy):
        self.funny = config.require_bool("newspaper.tone.funny")
        self.colorful = config.require_bool("newspaper.tone.colorful")
        self.allow_pointed = config.require_bool("newspaper.tone.allow_pointed_humor")
        self.disallow_snide = config.require_bool("newspaper.tone.disallow_snide_or_mean")
        self.forbidden = tuple(copy.tone()["forbidden_register"])

    def describe(self):
        return {
            "funny": self.funny,
            "colorful": self.colorful,
            "allow_pointed_humor": self.allow_pointed,
            "disallow_snide_or_mean": self.disallow_snide,
            "forbidden_register_terms": len(self.forbidden),
            "spec": "#30",
            "note": "The funny/colourful/pointed half of #30 is a judged criterion; "
                    "the snide-or-mean half has a mechanical floor, checked over this "
                    "edition's finished prose.",
        }

    @staticmethod
    def pattern_for(term):
        """The register term as a pattern that matches words, not substrings.

        A leading word boundary, and deliberately no trailing one. The register
        is written with stems in it -- ``humiliat`` is there to catch humiliate,
        humiliated and humiliating with one entry -- so anchoring the end would
        quietly disarm them. Anchoring only the start is what actually matters:
        without it ``loser`` fires inside "closer" and ``liar`` inside
        "familiar", and since the paper reprints exports exactly as mayors wrote
        them (that is the one string it must reproduce verbatim), an ordinary
        word in an ordinary offer would block the edition it appeared in.
        """
        escaped = re.escape(term.lower())
        return (r"\b" + escaped) if term[:1].isalnum() else escaped

    def findings(self, text):
        """Every forbidden term this text contains, with its context."""
        lowered = text.lower()
        found = []
        for term in self.forbidden:
            for match in re.finditer(self.pattern_for(term), lowered):
                start = max(match.start() - 40, 0)
                found.append(
                    {
                        "term": term,
                        "context": text[start:match.end() + 40].replace("\n", " "),
                    }
                )
        return found

    def check(self, text, where="edition"):
        """Raise if the finished prose trips the register and config says to care."""
        if not self.disallow_snide:
            return []
        found = self.findings(text)
        if found:
            raise RuleViolation(
                "%s trips content/newspaper.json's forbidden register, and "
                "config.newspaper.tone.disallow_snide_or_mean is true (spec #30): %r"
                % (where, found)
            )
        return found
