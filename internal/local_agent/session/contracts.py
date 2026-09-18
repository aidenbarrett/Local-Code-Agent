"""Small, strict conversation/controller boundary. Model text is never authority."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_MESSAGE_CHARS = 8_000


@dataclass(frozen=True)
class Proposal:
    kind: str
    text: str

    @classmethod
    def parse(cls, raw: str) -> "Proposal":
        # Reject extra fields, fences, batches and embedded policy/tool requests.
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate proposal field")
                result[key] = value
            return result

        if len(raw) > MAX_MESSAGE_CHARS + 100:
            raise ValueError("proposal exceeds size limit")
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {"kind", "text"}:
            raise ValueError("expected exactly kind and text")
        if value["kind"] not in ("reply", "repository", "self_check"):
            raise ValueError("unknown proposal kind")
        if not isinstance(value["text"], str) or not value["text"].strip():
            raise ValueError("proposal text must be a nonempty string")
        if len(value["text"]) > MAX_MESSAGE_CHARS:
            raise ValueError("proposal text exceeds size limit")
        return cls(value["kind"], value["text"].strip())


@dataclass(frozen=True)
class EvidenceRef:
    """Composite identity. evidence_id retains the worker's exact name:index."""
    task_id: str
    evidence_id: str


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    outcome: str
    answer: str
    # Historical observation at task completion, never proof for a later turn.
    verified_at_completion: bool = False
    evidence_ids: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return tuple(EvidenceRef(self.task_id, local_id) for local_id in self.evidence_ids)

    def render(self) -> str:
        # Composed here, in code, from typed fields. No model writes this line and
        # no model is asked to summarise it. The evidence count is included
        # because a claim nobody can count is not a checkable claim; the ids
        # remain canonical. Their task namespace is a separate field, never a
        # prefix consumers have to strip. Controller fields are not chat history.
        proof = "passed at task completion" if self.verified_at_completion else "not established"
        return (f"{self.answer}\n\n[Controller: {self.outcome}; verification: {proof}; "
                f"evidence: {len(self.evidence_ids)} item(s); task: {self.task_id}]\n"
                f"Evidence IDs: {', '.join(self.evidence_ids) or 'none'}")
