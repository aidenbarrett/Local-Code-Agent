"""Public claims recomputed from frozen rows instead of grepped from prose."""
from __future__ import annotations

import collections
import json
from pathlib import Path
import re
import sys


INTERNAL = Path(__file__).resolve().parents[2]
REPO = INTERNAL.parent
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from evaluation.endpoints import verified_completion  # noqa: E402

DATA = INTERNAL / "experiments" / "2026-09-08-30b-three-conditions-x3" / "data"
CONDITIONS = ("control", "narrow", "skill")
LEGACY_POOLED = {"control": (9, 30), "narrow": (22, 30), "skill": (23, 29)}
TYPED_POOLED = {"control": (1, 30), "narrow": (12, 30), "skill": (9, 29)}


def _files():
    files = sorted(DATA.glob("repeat-*.json"))
    assert files, f"no frozen repeat files under {DATA}"
    return files


def _counted_rows():
    grouped: dict[str, dict[str, list[dict]]] = collections.defaultdict(dict)
    for path in _files():
        payload = json.loads(path.read_text(encoding="utf-8"))
        repeat = path.name.split("-")[1]
        rows = [row for row in payload["rows"] if row.get("counted") is True]
        grouped[payload["condition"]][repeat] = rows
    return grouped


def _tally(rows, accounting):
    if accounting == "legacy":
        return sum(1 for row in rows if row.get("succeeded"))
    return sum(1 for row in rows if verified_completion(row))


def _readme_table():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    wanted = {
        "Control": "control",
        "Narrow tools": "narrow",
        "Narrow tools + written skill": "skill",
    }
    out = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3 or cells[0] not in wanted:
            continue
        per_repeat = [tuple(int(n) for n in pair.split("/")) for pair in re.findall(r"\d+/\d+", cells[1])]
        pooled = re.search(r"(\d+)/(\d+) \(([0-9.]+)\)", cells[2])
        assert pooled, f"could not parse pooled cell: {cells[2]!r}"
        out[wanted[cells[0]]] = {
            "per_repeat": per_repeat,
            "pooled": (int(pooled.group(1)), int(pooled.group(2))),
            "rate": float(pooled.group(3)),
        }
    assert set(out) == set(CONDITIONS)
    return out


def test_dataset_shape_matches_public_description():
    grouped = _counted_rows()
    assert set(grouped) == set(CONDITIONS)
    assert all(len(repeats) == 3 for repeats in grouped.values())
    attempted = sum(len(json.loads(path.read_text(encoding="utf-8"))["rows"]) for path in _files())
    assert attempted == 90
    counted = {condition: sum(len(rows) for rows in repeats.values()) for condition, repeats in grouped.items()}
    assert counted == {"control": 30, "narrow": 30, "skill": 29}


def test_readme_pooled_and_repeat_figures_recompute_from_legacy_gen1_rows():
    grouped = _counted_rows()
    published = _readme_table()
    for condition in CONDITIONS:
        repeats = sorted(grouped[condition].items())
        expected_repeat = [(_tally(rows, "legacy"), len(rows)) for _, rows in repeats]
        rows = [row for _, group in repeats for row in group]
        expected_pooled = (_tally(rows, "legacy"), len(rows))
        assert published[condition]["per_repeat"] == expected_repeat
        assert published[condition]["pooled"] == expected_pooled
        hits, total = expected_pooled
        assert abs(published[condition]["rate"] - hits / total) < 0.0005
        assert (sum(h for h, _ in expected_repeat), sum(t for _, t in expected_repeat)) == expected_pooled


def test_legacy_and_typed_accountings_are_both_pinned_and_diverge():
    grouped = _counted_rows()
    for condition in CONDITIONS:
        rows = [row for group in grouped[condition].values() for row in group]
        legacy = (_tally(rows, "legacy"), len(rows))
        typed = (_tally(rows, "typed"), len(rows))
        assert legacy == LEGACY_POOLED[condition]
        assert typed == TYPED_POOLED[condition]
        assert legacy != typed


def test_written_skill_effect_changes_sign_but_narrowing_direction_survives():
    def rate(table, condition):
        hits, total = table[condition]
        return hits / total

    legacy_skill = rate(LEGACY_POOLED, "skill") - rate(LEGACY_POOLED, "narrow")
    typed_skill = rate(TYPED_POOLED, "skill") - rate(TYPED_POOLED, "narrow")
    assert legacy_skill > 0
    assert typed_skill < 0
    assert legacy_skill * typed_skill < 0
    for table in (LEGACY_POOLED, TYPED_POOLED):
        assert rate(table, "narrow") > rate(table, "control")


def test_readme_names_historical_accounting_and_keeps_runtime_caveat():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "Generation-1 legacy weighted `succeeded` accounting" in text
    assert "typed E3 `verified_completion`" in text
    assert "small and unresolved" in text
    assert "NUC under WSL2 Ubuntu with llama.cpp" in text
