"""Every active file declares whether it is part of the instrument.

Hashed-surface membership is currently decided by where a file sits. Nothing states
the intent, so a reorganisation can move code out of the instrument, or pull code in
that was deliberately outside it, and the only symptom is a `source_sha256` that
quietly covers a different set of bytes than anyone believes.

That is not hypothetical. Until #58 the Session Hub schema was loaded at runtime by
the validator and was not hashed: a `maxLength` could be edited and source identity
did not move. The glob was added deliberately. The point of this test is that the next
one does not have to be noticed by a human first.

The declaration below is the intent. `provenance._files()` is the reality. This test
asserts they agree in **both** directions:

    classified INSTRUMENT     but not covered  ->  code left the instrument
    classified NOT_INSTRUMENT but covered      ->  code entered the instrument
    classified by nothing                      ->  somebody must decide
    classified by two rules equally            ->  the declaration is ambiguous

A new logical area therefore cannot be created without choosing a side, which is the
half of the membership rule that a glob alone cannot enforce.

Generated packaging metadata is not repository source. Editable installs create
`*.egg-info` under `internal/`; wheel-style metadata may use `*.dist-info`. Those
metadata directories are excluded explicitly rather than classified as a logical area,
so installing the package cannot make the repository appear to have gained source.

What this test does NOT do: it cannot tell you the choice was correct. If PR H creates
`internal/serving/` and declares it NOT_INSTRUMENT, this passes. What it guarantees is
that somebody wrote the word down, in a diff a reviewer reads, rather than a directory
name deciding it in silence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# provenance anchors itself at the repository root; match it rather than guessing.
ROOT = Path(__file__).resolve().parents[3]
INTERNAL = ROOT / "internal"
sys.path.insert(0, str(INTERNAL))

from local_agent import provenance  # noqa: E402

INSTRUMENT = "instrument"
NOT_INSTRUMENT = "not-instrument"
GENERATED_METADATA_SUFFIXES = (".egg-info", ".dist-info")

# A rule is (where, suffixes, classification, why).
#
#   where     a path ending in "/" claims that subtree; anything else is one exact file.
#   suffixes  None claims every file type. A tuple claims only those extensions, so a
#             file type nobody thought about stays unclassified and fails, rather than
#             being swept in by a broad pattern. That is the shape of the bug #58 fixed.
#
# Deliberately not globs. `fnmatch` treats `*` as matching separators, so
# `internal/evaluation/**/*.py` silently fails to match `internal/evaluation/oracle.py`
# and a rule that matches nothing classifies nothing. Prefix plus suffix cannot do that.
RULES: tuple[tuple[str, tuple[str, ...] | None, str, str], ...] = (
    # ---- instrument: bytes that change what the agent does or how it is measured
    ("internal/local_agent/", (".py",), INSTRUMENT,
     "the agent implementation"),
    ("internal/evaluation/", (".py",), INSTRUMENT,
     "what makes a result count"),
    ("internal/measurement/", (".py", ".sh"), INSTRUMENT,
     "qualification and experiment launch policy, including its shell entry points"),
    ("internal/skills/", None, INSTRUMENT,
     "every file under a skill is model-facing, loaded on demand"),
    ("internal/benchmark_fixture/cpp_project/", None, INSTRUMENT,
     "the task the agent is measured on"),
    ("internal/docs/session-contract/v1/", (".json",), INSTRUMENT,
     "the versioned wire contract the validator loads at runtime"),

    # ---- not instrument: everything whose edit must not move the agent's identity
    ("internal/tests/", None, NOT_INSTRUMENT,
     "tests observe the instrument; hashing them would make every new test a new "
     "instrument"),
    ("internal/scripts/", None, NOT_INSTRUMENT,
     "operator and product-help tools, outside the measured surface by intent"),
    ("internal/devtools/", None, NOT_INSTRUMENT,
     "developer utilities that inspect the repository. They analyse the instrument and "
     "are not part of it: a change to the pre-flight audit must not move the identity "
     "of the thing it audits"),
    ("internal/docs/", None, NOT_INSTRUMENT,
     "prose. The versioned contract under session-contract/v1 is the exception above"),
    ("internal/experiments/", None, NOT_INSTRUMENT,
     "frozen evidence. Hashing collected runs would let old results move the "
     "identity of the instrument that produced them"),
    ("internal/personas/", None, NOT_INSTRUMENT,
     "chat persona configuration, deliberately outside the instrument"),
    ("internal/ui/", None, NOT_INSTRUMENT,
     "presentation assets and themes only; they do not grant authority, execute work, "
     "or alter measurement/evaluation semantics"),
    ("internal/benchmark_fixture/", None, NOT_INSTRUMENT,
     "the fixture generator produces cpp_project; only its output is measured"),
    ("internal/bootstrap-work-laptop-core.ps1", None, NOT_INSTRUMENT,
     "existing Windows bootstrap helper; exact path so a new root script must be classified"),
    ("internal/bootstrap-work-laptop.ps1", None, NOT_INSTRUMENT,
     "existing Windows bootstrap entry point; exact path so a new root script must be classified"),
    ("internal/work-laptop-one-shot.ps1", None, NOT_INSTRUMENT,
     "existing Windows bootstrap entry point; exact path so a new root script must be classified"),
    ("internal/README.md", None, NOT_INSTRUMENT,
     "navigation prose for the internal tree; exact path so a new root document must be classified"),
    ("internal/INSTRUMENT.json", None, NOT_INSTRUMENT,
     "a declaration about the instrument, not an input to it"),
    ("internal/terminal_ui.py", None, NOT_INSTRUMENT,
     "presentation only. Named file by file on purpose: a new module at the "
     "internal/ root should fail this test until somebody classifies it"),
)

# Top-level areas under internal/. A new one fails `test_every_active_file_is_classified`
# already; this list makes the intent readable and the failure message specific.
DECLARED_AREAS = frozenset({
    "benchmark_fixture", "devtools", "docs", "evaluation", "experiments", "local_agent",
    "measurement", "personas", "scripts", "skills", "tests", "ui",
})


def _is_generated_metadata(path: Path) -> bool:
    """True only for standard packaging-metadata directories, never source areas."""
    try:
        parts = path.relative_to(INTERNAL).parts
    except ValueError:
        return False
    return any(part.endswith(GENERATED_METADATA_SUFFIXES) for part in parts)


def _claims(rule, rel: str) -> bool:
    where, suffixes, _, _ = rule
    if where.endswith("/"):
        if not rel.startswith(where):
            return False
    elif rel != where:
        return False
    return suffixes is None or rel.endswith(suffixes)


def _specificity(rule) -> tuple[int, int]:
    """Longer path wins; among equal paths, a suffix-restricted rule wins."""
    where, suffixes, _, _ = rule
    return (len(where), 1 if suffixes is not None else 0)


def _matches(rel: str) -> list:
    return [rule for rule in RULES if _claims(rule, rel)]


def classify(rel: str):
    """The winning rule for one path, or None when nothing claims it unambiguously."""
    hits = _matches(rel)
    if not hits:
        return None
    best = max(_specificity(rule) for rule in hits)
    winners = [rule for rule in hits if _specificity(rule) == best]
    return winners[0] if len(winners) == 1 else None


def active_files() -> list[Path]:
    """Repository files under internal/, excluding generated/runtime metadata.

    Deliberately every file type, not only .py. The schema that escaped the hashed
    surface until #58 was JSON. Packaging metadata generated by installation is not
    repository source and is excluded explicitly.
    """
    found = []
    for path in INTERNAL.rglob("*"):
        if not path.is_file():
            continue
        if provenance._SKIP.intersection(path.parts):
            continue
        if _is_generated_metadata(path):
            continue
        found.append(path)
    return sorted(found)


def rel_of(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.fixture(scope="module")
def covered() -> set[Path]:
    return {path.resolve() for path in provenance._files()}


def test_every_active_file_is_classified():
    unclassified = [rel_of(p) for p in active_files() if not _matches(rel_of(p))]
    assert not unclassified, (
        "these files are in the tree and no rule says whether they are part of the "
        "instrument. Add a rule to RULES in this file, and if the answer is "
        "'instrument', add the matching glob to provenance._HASHED in the same "
        f"commit:\n  " + "\n  ".join(unclassified))


def test_no_file_is_claimed_by_two_rules_of_equal_specificity():
    ambiguous = []
    for path in active_files():
        rel = rel_of(path)
        hits = _matches(rel)
        if not hits:
            continue
        best = max(_specificity(rule) for rule in hits)
        winners = [rule[0] for rule in hits if _specificity(rule) == best]
        if len(winners) > 1:
            ambiguous.append(f"{rel} <- {winners}")
    assert not ambiguous, (
        "two rules claim the same file with equal specificity, so the declaration "
        "does not decide anything:\n  " + "\n  ".join(ambiguous))


def test_files_declared_instrument_are_actually_hashed(covered):
    """Catches a reorganisation that moved instrument code out of the surface."""
    missing = []
    for path in active_files():
        rule = classify(rel_of(path))
        if rule and rule[2] == INSTRUMENT and path.resolve() not in covered:
            missing.append(f"{rel_of(path)}  (declared instrument: {rule[3]})")
    assert not missing, (
        "declared part of the instrument but not covered by provenance._HASHED. "
        "Either the code moved and its glob did not follow it, or the declaration "
        f"is wrong:\n  " + "\n  ".join(missing))


def test_files_declared_not_instrument_are_not_hashed(covered):
    """Catches code silently entering the instrument via a broadened glob."""
    unexpected = []
    for path in active_files():
        rule = classify(rel_of(path))
        if rule and rule[2] == NOT_INSTRUMENT and path.resolve() in covered:
            unexpected.append(f"{rel_of(path)}  (declared outside: {rule[3]})")
    assert not unexpected, (
        "covered by provenance._HASHED but declared outside the instrument. A glob "
        "reached further than intended, and every edit to these files now moves "
        f"source_sha256:\n  " + "\n  ".join(unexpected))


def test_hashed_surface_does_not_reach_outside_internal(covered):
    """A glob that escapes internal/ would make unrelated repository files behavioural."""
    allowed_outside = {(ROOT / name).resolve() for name in provenance._HASHED_FILES}
    internal = INTERNAL.resolve()
    strays = [
        str(path) for path in covered
        if internal not in path.parents and path not in allowed_outside
    ]
    assert not strays, (
        "provenance covers files outside internal/ that are not in _HASHED_FILES:\n  "
        + "\n  ".join(sorted(strays)))


def test_top_level_areas_match_the_declaration():
    present = {
        child.name for child in INTERNAL.iterdir()
        if child.is_dir()
        and child.name not in provenance._SKIP
        and not _is_generated_metadata(child)
    }
    added = sorted(present - DECLARED_AREAS)
    removed = sorted(DECLARED_AREAS - present)
    assert not added, (
        "new top-level area under internal/. Classify it in RULES and list it in "
        f"DECLARED_AREAS, in the same commit that creates it: {added}")
    assert not removed, (
        "DECLARED_AREAS names areas that no longer exist. Remove them so this list "
        f"keeps meaning something: {removed}")


def test_generated_packaging_metadata_is_not_source_classification():
    assert _is_generated_metadata(INTERNAL / "local_code_agent.egg-info" / "PKG-INFO")
    assert _is_generated_metadata(INTERNAL / "package.dist-info" / "METADATA")
    assert not _is_generated_metadata(INTERNAL / "new_area" / "module.py")


def test_the_classification_is_not_vacuous():
    """A rules table that classified nothing would pass every assertion above."""
    files = active_files()
    assert len(files) > 100, f"expected a populated tree, found {len(files)} files"
    kinds = {classify(rel_of(p))[2] for p in files if classify(rel_of(p))}
    assert kinds == {INSTRUMENT, NOT_INSTRUMENT}, (
        f"both classifications must be in use, found {sorted(kinds)}")


def test_every_rule_matches_something():
    """A rule that matches nothing is either a typo or a leftover after a move."""
    rels = [rel_of(p) for p in active_files()]
    dead = [f"{rule[0]} {rule[1] or '(all types)'}" for rule in RULES
            if not any(_claims(rule, rel) for rel in rels)]
    assert not dead, (
        "these rules match no file in the tree. A rule that matches nothing "
        "classifies nothing, and will not fail when the path it was written for "
        f"comes back:\n  " + "\n  ".join(dead))
