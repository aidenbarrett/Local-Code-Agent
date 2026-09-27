"""The durable candidate facts carried in a retained task result (lca.task-result/2).

One owner for the shape. Writers project it from controller-set typed metrics only;
readers validate it strictly. Worker prose never contributes. A Session Hub projection
can fold results by `candidate_task_id` to know whether a candidate is ready,
applied, undone or committed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

ROLES = frozenset({"prepared", "applied", "apply_refused", "undone", "committed", "discarded"})
_KEYS = frozenset({
    "role", "candidate_task_id", "retained", "paths", "patch_sha256", "base_commit", "commit",
})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_MAX_PATHS = 512


@dataclass(frozen=True)
class CandidateFacts:
    role: str
    candidate_task_id: str
    retained: bool
    paths: tuple[str, ...]
    patch_sha256: str | None
    base_commit: str | None
    commit: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "candidate_task_id": self.candidate_task_id,
            "retained": self.retained,
            "paths": list(self.paths),
            "patch_sha256": self.patch_sha256,
            "base_commit": self.base_commit,
            "commit": self.commit,
        }


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("candidate_task_id must be a UUID string")
    canonical = str(UUID(value))
    if canonical != value:
        raise ValueError("candidate_task_id must be a canonical lowercase UUID")
    return canonical


def validate_candidate(value: object) -> CandidateFacts | None:
    """Strictly validate a retained `candidate` value; None stays None."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ValueError("retained candidate facts have an unexpected shape")
    role = value["role"]
    if role not in ROLES:
        raise ValueError(f"retained candidate role is not reviewed: {role!r}")
    if not isinstance(value["retained"], bool):
        raise ValueError("retained candidate 'retained' must be boolean")
    if role != "prepared" and value["retained"]:
        raise ValueError("only a prepared candidate can be retained")
    paths = value["paths"]
    if not isinstance(paths, list) or len(paths) > _MAX_PATHS or not all(
        isinstance(p, str) and 0 < len(p) <= 4096 for p in paths
    ):
        raise ValueError("retained candidate paths are invalid")
    for key, pattern in (("patch_sha256", _HEX64), ("base_commit", _GIT_OID), ("commit", _GIT_OID)):
        item = value[key]
        if item is not None and (not isinstance(item, str) or not pattern.fullmatch(item)):
            raise ValueError(f"retained candidate {key} is invalid")
    if (role == "committed") != (value["commit"] is not None):
        raise ValueError("a commit id is present exactly when the role is committed")
    if value["retained"] and (not paths or value["patch_sha256"] is None):
        raise ValueError("a retained candidate must name its paths and patch")
    return CandidateFacts(
        role=role,
        candidate_task_id=_canonical_uuid(value["candidate_task_id"]),
        retained=value["retained"],
        paths=tuple(paths),
        patch_sha256=value["patch_sha256"],
        base_commit=value["base_commit"],
        commit=value["commit"],
    )


def _oid(value: object) -> str | None:
    return value if isinstance(value, str) and _GIT_OID.fullmatch(value) else None


def project_candidate(task_id: str, succeeded: bool, metrics: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project controller-set typed metrics into the durable candidate block, or None."""
    prepared = metrics.get("candidate")
    if isinstance(prepared, Mapping):
        retained = prepared.get("retained") is True
        facts = CandidateFacts(
            role="prepared",
            candidate_task_id=str(UUID(task_id)),
            retained=retained,
            paths=tuple(prepared.get("paths") or ()) if retained else (),
            patch_sha256=prepared.get("patch_sha256") if retained else None,
            base_commit=_oid(prepared.get("base_commit")),
            commit=None,
        )
        validate_candidate(facts.as_payload())
        return facts.as_payload()

    for key, ok_role, bad_role in (
        ("candidate_import", "applied", "apply_refused"),
        ("candidate_undo", "undone", None),
        ("candidate_commit", "committed", None),
        ("candidate_discard", "discarded", None),
    ):
        action = metrics.get(key)
        if not isinstance(action, Mapping) or not action.get("candidate_task_id"):
            continue
        if key == "candidate_import":
            role = ok_role if action.get("applied") and action.get("import_verified") else bad_role
        else:
            role = ok_role if succeeded else None
        if role is None:
            return None
        facts = CandidateFacts(
            role=role,
            candidate_task_id=str(action["candidate_task_id"]),
            retained=False,
            paths=tuple(action.get("paths") or ()),
            patch_sha256=action.get("patch_sha256"),
            base_commit=None,
            commit=_oid(action.get("commit")) if role == "committed" else None,
        )
        validate_candidate(facts.as_payload())
        return facts.as_payload()
    return None


RESULT_SCHEMA_V1 = "lca.task-result/1"
RESULT_SCHEMA = "lca.task-result/2"
RESULT_FIELDS_V1 = frozenset({
    "schema", "task_id", "outcome", "terminal_state", "verdict",
    "verification_ran", "verified_at_completion", "evidence_ids", "answer",
})
RESULT_FIELDS = RESULT_FIELDS_V1 | {"candidate"}


def result_fields_for(schema: object) -> frozenset[str]:
    """The exact field set a retained result of this schema must have."""
    if schema == RESULT_SCHEMA:
        return RESULT_FIELDS
    if schema == RESULT_SCHEMA_V1:
        return RESULT_FIELDS_V1
    raise ValueError(f"unknown retained task result schema: {schema!r}")


__all__ = [
    "CandidateFacts", "RESULT_FIELDS", "RESULT_SCHEMA", "RESULT_SCHEMA_V1", "ROLES",
    "project_candidate", "result_fields_for", "validate_candidate",
]
