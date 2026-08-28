"""The whole game, end to end, in one command.

    python3 -m playtest.run              # replay, publish, build the site, report
    python3 -m playtest.run --check      # report only; write nothing
    python3 -m playtest.run --json       # the report as JSON

Five steps, in the order a real game night actually happens in:

1. **replay** the recorded game through the engine (:mod:`playtest.replay`);
2. **verify** that the recorded game is the game the mayors were briefed on --
   see :func:`assert_schedule_matches`, which is the one integrity check this
   module owns rather than borrows;
3. **publish** every edition to ``editions/`` (:func:`newspaper.publish_game`);
4. **build** the site at the paper's private address (:func:`hosting.build_site`);
5. **check** all thirty-five requirements against the result at once
   (:mod:`playtest.conformance`).

Steps 3 and 4 are not decoration. Half the requirements in step 5 -- the
archive, the identity rules, the images, the exposure policy -- are properties
of *published bytes*, and there is no way to check a published byte without
publishing it.
"""

import json
import os
import sys

import hosting
from engine.config import repo_root
from hosting import identity as identity_module
from newspaper.edition import Paper
from newspaper.publish import publish_game

from . import conformance
from .replay import replay
from .transcript import StandIns, load_transcript

def _label(config):
    """Where this game's rendered editions land, beside the sample run's.

    A label rather than a directory: :mod:`newspaper.publish` takes the parent
    from ``config.newspaper.output.editions_dir``.
    """
    return config.require_str("playtest.editions_label")


def _site_dir(config, root=None):
    """This game's own published address, which is not the live game's.

    Spec #27 makes an address an append-only archive -- an edition published
    there stays there -- so two different games cannot be built into one
    directory without one of them proposing to delete the other's back issues.
    The guard in :mod:`hosting.guard` says so out loud, which is how this was
    found. So the recorded game gets ``config.playtest.site_dir`` and the live
    game keeps ``hosting.site_dir``; the build, the manifest and the privacy
    policy are identical, and only the directory differs.
    """
    return os.path.join(root or repo_root(), config.require_str("playtest.site_dir"))


def assert_schedule_matches(game, journal):
    """The recorded game must be the game the mayors were briefed on.

    Each mayor's agent was shown, in advance, the notices their check-ins would
    put in front of them -- which is the only way anybody can write an export.
    That briefing came from a stand-in pass through this same engine, and it is
    only honest if the *schedule* is a function of the seed and the seating plan
    rather than of anything the mayors wrote.

    It is: which need a city draws depends on the seed, the city and the
    categories that city has already had, and nothing else; when a rotation
    closes depends on who is in the queue. So a stand-in game and the real game
    must agree, need for need and round for round, and they differ only in what
    was said and who won. This asserts exactly that, because if it ever stopped
    being true the briefs would be describing a game nobody played.
    """
    reference, _ = replay(StandIns(), config=game.config, content=game.content)
    ours = {
        key: (need.importing_city, need.category, need.opened_round, need.closed_round)
        for key, need in game.needs.items()
    }
    theirs = {
        key: (need.importing_city, need.category, need.opened_round, need.closed_round)
        for key, need in reference.needs.items()
    }
    if ours != theirs or sorted(game.rounds) != sorted(reference.rounds):
        raise AssertionError(
            "the recorded game's schedule no longer matches the one the mayors were "
            "briefed on; the briefs describe a game nobody played. ours=%r theirs=%r"
            % (sorted(ours.items()), sorted(theirs.items()))
        )
    return True


def read_public_files(public_root):
    """Every byte the site actually serves, as text, keyed by filename."""
    files = {}
    for name in sorted(os.listdir(public_root)):
        path = os.path.join(public_root, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            files[name] = fh.read().decode("utf-8", "replace")
    return files


def play(write=True, label=None, root=None):
    """Play the recorded game and check everything about the result.

    Returns ``(game, journal, report, artifacts)``. With ``write=False`` the
    editions and the site are still *built* -- they have to be, or half the
    requirements could not be checked -- but into a temporary directory, so a
    check run leaves the repository alone.
    """
    transcript = load_transcript(root=root)
    game, journal = replay(transcript)
    assert_schedule_matches(game, journal)

    paper = Paper(game)
    identity = identity_module.load_or_create(game.config, root=root)
    label = label if label is not None else _label(game.config)

    if write:
        editions = publish_game(game, label=label, paper=paper)
        site = hosting.build_site(
            game, out_dir=_site_dir(game.config, root), paper=paper,
            identity=identity, root=root,
        )
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            editions = publish_game(
                game, label=label, out_dir=os.path.join(tmp, "editions"), paper=paper
            )
            site = hosting.build_site(
                game, out_dir=os.path.join(tmp, "site"), paper=paper,
                identity=identity, root=root,
            )
            artifacts = _artifacts(paper, editions, site, identity, transcript)
            return game, journal, conformance.run(game, journal, artifacts), artifacts

    artifacts = _artifacts(paper, editions, site, identity, transcript)
    return game, journal, conformance.run(game, journal, artifacts), artifacts


def _artifacts(paper, editions, site, identity, transcript):
    return {
        "archive": paper.archive(),
        "editions": editions,
        "site": site,
        "site_id": identity.site_id,
        "public_files": read_public_files(site["public_root"]),
        "transcript_data": transcript.data,
    }


def report_path(root=None):
    return os.path.join(root or repo_root(), "playtest", "conformance.json")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    write = "--check" not in argv
    game, journal, report, artifacts = play(write=write)

    if "--json" in argv:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 1 if report.failures else 0

    print(
        "Sister Cities -- %d mayors, %d rounds, %d import needs, %d offers"
        % (len(game.players), len(game.rounds), len(game.needs), len(game.submissions))
    )
    print(
        "  editions -> %s\n  site     -> %s (%d files at the private address)"
        % (
            os.path.relpath(artifacts["editions"]["directory"], repo_root()),
            os.path.relpath(artifacts["site"]["public_root"], repo_root()),
            len(artifacts["public_files"]),
        )
    )
    print("\nspec conformance, all thirty-five at once:")
    print(report.to_text())
    counts = report.to_dict()["counts"]
    print(
        "\n%d checked: %s"
        % (len(report.findings), ", ".join(
            "%d %s" % (count, status) for status, count in sorted(counts.items())
        ))
    )
    if report.judged:
        print(
            "the %d judged findings carry their evidence and are for the Evaluator, "
            "not for this script" % len(report.judged)
        )
    if write:
        with open(report_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n")
        print("report   -> playtest/conformance.json")
    return 1 if report.failures else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
