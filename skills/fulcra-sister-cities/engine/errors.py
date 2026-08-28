"""Exception types for the Sister Cities engine.

Every rule the engine enforces raises a *named* error rather than returning a
bool, so a caller (and a test) can tell "this was rejected because of spec #15's
submission cap" apart from "this was rejected because the round was over".
"""


class GameError(Exception):
    """Base class for everything this package raises."""


# --- configuration -------------------------------------------------------

class ConfigError(GameError):
    """config.json is missing, malformed, or missing a key the engine needs."""


class MissingConfigKey(ConfigError):
    def __init__(self, dotted_path, source):
        super().__init__(
            "config.json is missing required key %r (source: %s). Spec's "
            "Generation Rules require every configurable parameter to live in "
            "config.json; the engine deliberately has no inline default to "
            "fall back on." % (dotted_path, source)
        )
        self.dotted_path = dotted_path
        self.source = source


class ConfigTypeError(ConfigError):
    pass


# --- content -------------------------------------------------------------

class ContentError(GameError):
    """A content/*.json file is missing or does not match its expected shape."""


class NoEligibleImportNeed(GameError):
    """No import need is left that satisfies the repetition rule (spec #14).

    Raised rather than silently relaxing the rule: relaxing it is a config
    decision (``imports.allow_repeat_category_for_same_city``), not something
    the engine may decide on its own mid-game.
    """


# --- rules ---------------------------------------------------------------

class RuleViolation(GameError):
    """An action that the game's rules do not permit."""


class RosterError(RuleViolation):
    """Registration/roster problem (player count, unknown player, ...)."""


class DuplicateCity(RuleViolation):
    """Two players picked the same city (spec #2).

    ``register_player`` refuses the collision; it does not resolve it.
    Resolution -- reassignment to a geographically close alternative from
    ``content/gazetteer.json`` -- is :func:`engine.join.join_player`, which
    catches this error and walks ``self.alternatives``. Keeping the two apart is
    deliberate: the low-level seat must never quietly move a mayor to a
    different city, and the joining player must never simply be told "no".
    """

    def __init__(self, requested, normalized, held_by, alternatives=()):
        super().__init__(
            "city %r (normalized %r) is already held by player %r"
            % (requested, normalized, held_by)
        )
        self.requested = requested
        self.normalized = normalized
        self.held_by = held_by
        self.alternatives = list(alternatives)


class PhaseError(RuleViolation):
    """The action is legal in principle but not in the game's current phase."""


class SubmissionRejected(RuleViolation):
    """An export submission the rules do not accept (spec #15)."""


class PickRejected(RuleViolation):
    """A winner pick the rules do not accept (spec #18)."""


class CheckInExhausted(RuleViolation):
    """The player already used their one check-in this round (spec #11)."""


class BlindVotingViolation(GameError):
    """A view or payload would expose an exporter's identity (spec #18, #21).

    Raised by the audit in ``engine.audit``. Reaching this means a *code* bug,
    not a player mistake -- it is a tripwire, not a game rule.
    """


class ExposurePolicyViolation(GameError):
    """A payload publishes something config.json says to withhold (spec #22).

    The sibling of :class:`BlindVotingViolation`, and the distinction between
    them is the point: this one is about a *configured* exposure decision being
    ignored, while that one is about the exporter anonymity that spec #21 puts
    beyond configuration entirely.
    """
