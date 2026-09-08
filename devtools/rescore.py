#!/usr/bin/env python3
"""Re-score a completed dataset under the current evaluator. Never in place.

    python devtools/rescore.py --dataset run.json --out run.rescored.json

A tightened evaluator raises an obvious question about every number collected
under the old one, and the honest way to answer it is to run the new rules over
the old rows and publish both. What this must never become is a way to quietly
improve a result: the source is read only, the output is a separate file, and
it records what produced it.

The derived artifact names its source by sha256, names the evaluator by the
package hash, and gives the original and rescored verdict for every row with
the reason for any change. If the source and the rescore disagree about a row,
that disagreement is the finding, not an inconvenience to smooth over.

Rows carry enough to re-run every check offline: the tool history with typed
execution and domain verdicts, arguments, mutation epochs, the claim and the
answer. Checks that need something a row does not carry are reported as
unavailable rather than guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "evals"))

from eval_cases import CASES, SUCCESS_THRESHOLD  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402

BY_NAME = {c.name: c for c in CASES}

# Everything a Check may touch. A row that lacks any of these cannot be
# rescored, and saying so is the only correct answer.
NEEDED = ("history", "mutation_epoch", "claim", "tool_calls", "halt_reason", "answer")

# Structured facts some required checks read out of a tool result. A row from
# before they were persisted lets a check see an empty dict and conclude
# "absent, therefore fine", which is not the same as knowing. A row missing
# them is partially replayable: the checks that need them are unknown, not
# passed.
#
# The list grows when the classifier learns a new way for a pass to be
# worthless, and every dataset collected before that day becomes partially
# replayable at that moment. That is the correct and uncomfortable answer:
# `no_build_record` was added after the frozen Slice 3 datasets were taken, so
# nothing in them records whether the tree had ever been built, and a re-score
# that read the absent key as False would confidently reproduce the exact bug
# the key exists to catch.
EVIDENCE_DEPENDENT = {
    "run_test": (
        "totals",
        "stale_sources",
        "no_build_record",
        "build_profile_mismatch",
        "profile_mismatch",
    )
}


def _replay(row: dict):
    """A stand-in RunResult built only from what the row recorded."""
    history = [types.SimpleNamespace(**h) for h in row["history"]]
    state = types.SimpleNamespace(
        history=history,
        mutation_epoch=row["mutation_epoch"],
        claim=row["claim"],
        tool_calls=row["tool_calls"],
        halt_reason=row["halt_reason"],
        verified=row.get("verified", False),
    )
    return types.SimpleNamespace(state=state, answer=row.get("answer") or "")


def _replayability(row: dict) -> tuple[str, list[str]]:
    """How much of this row's verdict can honestly be recomputed."""
    missing = [k for k in NEEDED if k not in row]
    if missing:
        return "not_replayable", [f"row lacks {', '.join(missing)}"]
    gaps: list[str] = []
    for call in row.get("history", []):
        wanted = EVIDENCE_DEPENDENT.get(call.get("name"), ())
        if not wanted:
            continue
        evidence = call.get("evidence")
        # A key that is absent is not a key that is False. The whole point of
        # these fields is to say "this pass proves nothing", so a row that
        # never recorded one cannot be re-scored as though it had recorded a
        # clean one.
        absent = [k for k in wanted if not isinstance(evidence, dict) or k not in evidence]
        if absent:
            gaps.append(f"{call['name']} calls do not record {', '.join(absent)}, "
                        f"so checks that read them are unknown, not passed")
            break
    return ("partially_replayable" if gaps else "fully_replayable"), gaps


def rescore_row(row: dict) -> dict:
    replayability, gaps = _replayability(row)
    if replayability == "not_replayable":
        return {"case": row.get("case"), "rescorable": False,
                "replayability": replayability, "reason": "; ".join(gaps)}
    case = BY_NAME.get(row["case"])
    if case is None:
        return {"case": row.get("case"), "rescorable": False,
                "replayability": "not_replayable",
                "reason": "no case of that name in the current evaluator"}

    result = _replay(row)
    required = {k: bool(fn(result)) for k, fn in case.required_checks.items()}
    quality = {k: bool(fn(result)) for k, fn in case.checks.items()}
    score = sum(quality.values()) / len(quality) if quality else 0.0
    claim_ok = result.state.claim in case.expected_claim
    scope = any(
        h.name in case.forbidden_tools and getattr(h, "reason", None) != "tool_not_allowed"
        for h in result.state.history
    )
    # The old evaluator's `outcome` is deliberately NOT an input. It is the
    # verdict being re-examined, and letting it gate the new one means a row
    # the old rules wrongly failed can never be shown to pass, which is exactly
    # the navigation and review-restraint case. What is used instead is the
    # halt, which is a fact about the run rather than a judgement about it.
    halted = row.get("halt_reason") is not None
    succeeded = bool(
        not halted and all(required.values()) and claim_ok
        and score >= SUCCESS_THRESHOLD and not scope
        and not row.get("oracle_tampered") and not row.get("verification_disagreement")
    )

    why = [k for k, ok in required.items() if not ok]
    if not claim_ok:
        why.append(f"claim {result.state.claim!r} is not in the case contract")
    if scope:
        why.append("out-of-scope mutation")
    if score < SUCCESS_THRESHOLD:
        why.append(f"quality {score:.2f} below {SUCCESS_THRESHOLD}")
    if halted:
        why.append(f"halted: {row.get('halt_reason')}")

    # A definitive verdict needs every fact the verdict depends on. Labelling a
    # row partial and then publishing a capability bit computed from the
    # missing half is the absent-versus-unknown error the label was added to
    # prevent: `_whole_suite_passed` saw no evidence dict, found no stale
    # sources and no zero total in it, and passed.
    definitive = replayability == "fully_replayable"
    return {
        "case": row["case"], "rescorable": True,
        "replayability": replayability,
        "replay_gaps": gaps,
        "definitive": definitive,
        "original_succeeded": row.get("succeeded"),
        "rescored_succeeded": succeeded if definitive else None,
        "provisional_succeeded": succeeded if not definitive else None,
        "changed": (row.get("succeeded") != succeeded) if definitive else None,
        "original_score": row.get("score"),
        "rescored_quality_score": round(score, 3),
        "required_checks": required,
        "quality_checks": quality,
        "claim": result.state.claim,
        "claim_ok": claim_ok,
        "scope_violation": scope,
        "reasons": why,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = Path(args.dataset).read_bytes()
    data = json.loads(raw)
    rows = [rescore_row(r) for r in data.get("rows", [])]
    scorable = [r for r in rows if r["rescorable"]]
    partial = [r for r in scorable if r.get("replayability") == "partially_replayable"]
    definite = [r for r in scorable if r.get("definitive")]

    evaluator = package_identity()
    source_prompt = (data.get("package") or {}).get("base_prompt_sha256")
    protocol_changed = (
        source_prompt is not None and source_prompt != evaluator["base_prompt_sha256"]
    )
    protocol_unknown = source_prompt is None

    derived = {
        "kind": "rescore",
        # A re-score compares two evaluators. If the PROMPT also changed, it is
        # comparing two experiments, and a row that fails only on the finishing
        # protocol says nothing about the model that produced it.
        "protocol_changed": protocol_changed,
        "protocol_comparable": not (protocol_changed or protocol_unknown),
        "source_base_prompt_sha256": source_prompt,
        "source_dataset": str(Path(args.dataset).name),
        "source_dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "source_label": data.get("label"),
        "source_condition": data.get("condition"),
        "source_package": data.get("package"),
        "evaluator": evaluator,
        "success_threshold": SUCCESS_THRESHOLD,
        "rows_total": len(rows),
        "rows_rescorable": len(scorable),
        "rows_partially_replayable": len(partial),
        # What this tool does and does not do. It recomputes the evaluator's
        # checks against a recorded history. It does not re-run the agent, so
        # it cannot recover a decision the model never made.
        "operation": "recompute evaluator checks from a recorded history",
        "rows_changed": sum(1 for r in definite if r["changed"]),
        # Denominators count only rows whose verdict is fully supported.
        "capability_denominator": len(definite),
        "original_capability": sum(1 for r in definite if r["original_succeeded"]),
        "rescored_capability": sum(1 for r in definite if r["rescored_succeeded"]),
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(derived, indent=2) + "\n")

    print(f"source      {derived['source_dataset']}  sha256 {derived['source_dataset_sha256'][:16]}")
    if protocol_unknown:
        print("WARNING     the source records no base_prompt_sha256, so it cannot be "
              "shown that\n            the model was told the same thing. Treat "
              "protocol-related changes as\n            not comparable.")
    elif protocol_changed:
        print("WARNING     the base prompt changed between the source and this "
              "evaluator. Rows\n            that fail only on the finishing "
              "protocol are not model failures.")
    print(f"evaluator   {derived['evaluator']['source_sha256'][:16]}")
    print(f"rescorable  {derived['rows_rescorable']}/{derived['rows_total']}"
          + (f", {len(partial)} only partially" if partial else ""))
    for r in partial:
        print(f"  {r['case']:24s} PARTIAL: {'; '.join(r['replay_gaps'])}")
    print(f"capability  {derived['original_capability']} -> "
          f"{derived['rescored_capability']} of {derived['capability_denominator']} "
          f"fully replayable row(s)")
    for r in rows:
        if not r["rescorable"]:
            print(f"  {r['case']:24s} NOT RESCORABLE: {r['reason']}")
        elif r.get("changed"):
            print(f"  {r['case']:24s} {r['original_succeeded']} -> {r['rescored_succeeded']}"
                  f"   {'; '.join(r['reasons']) or 'now meets every requirement'}")
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
