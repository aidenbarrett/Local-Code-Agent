"""Validated fixed watch-job identity and canonical revision hashing.

A watch job is configuration, not an autonomous prompt. Its revision describes the
procedure being run; observed source HEAD/content are attempt data and therefore do
not participate in the job revision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from uuid import UUID


DELTA_SCHEMA_V1 = "lca.watch.delta/1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SourceSelection(str, Enum):
    LOCAL_REF = "local_ref"
    FIXED_ROOT = "fixed_root"


class DirtyTreePolicy(str, Enum):
    REFUSE = "refuse"
    ALLOW_READONLY = "allow_readonly"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha256(name: str, value: str | None, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _unique_text(values: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{name} entries must be nonempty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} entries must be unique")
    return tuple(sorted(values))


@dataclass(frozen=True)
class FixedWatchJob:
    job_id: str
    display_name: str
    repository_id: str
    repository_host: str
    source_selection: SourceSelection | str
    source_selector: str
    dirty_tree_policy: DirtyTreePolicy | str
    command_profile: str
    environment_allowlist: tuple[str, ...]
    verification_contract_sha256: str
    scope: tuple[str, ...]
    timeout_seconds: int
    skill_contract_sha256: str | None = None
    endpoint_policy: str | None = None
    delta_schema_version: str = DELTA_SCHEMA_V1

    def __post_init__(self) -> None:
        try:
            UUID(self.job_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("watch job id must be a UUID") from exc
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("watch display name must be nonempty")
        for name, value in (
            ("repository_id", self.repository_id),
            ("repository_host", self.repository_host),
            ("source_selector", self.source_selector),
            ("command_profile", self.command_profile),
            ("delta_schema_version", self.delta_schema_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        if not isinstance(self.timeout_seconds, int) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds < 1:
            raise ValueError("watch timeout_seconds must be a positive integer")

        object.__setattr__(self, "source_selection", SourceSelection(self.source_selection))
        object.__setattr__(self, "dirty_tree_policy", DirtyTreePolicy(self.dirty_tree_policy))

        env = _unique_text(tuple(self.environment_allowlist), name="environment allowlist")
        for variable in env:
            if _ENV_NAME.fullmatch(variable) is None:
                raise ValueError("environment allowlist contains an invalid variable name")
        object.__setattr__(self, "environment_allowlist", env)
        object.__setattr__(self, "scope", _unique_text(tuple(self.scope), name="watch scope"))

        _require_sha256("verification_contract_sha256", self.verification_contract_sha256)
        _require_sha256("skill_contract_sha256", self.skill_contract_sha256, optional=True)
        if self.endpoint_policy is not None and (
            not isinstance(self.endpoint_policy, str) or not self.endpoint_policy.strip()
        ):
            raise ValueError("endpoint_policy must be nonempty when present")

    def revision_payload(self) -> dict[str, object]:
        """Procedure identity only; excludes job id, label, schedule and observed source."""
        return {
            "command_profile": self.command_profile,
            "delta_schema_version": self.delta_schema_version,
            "dirty_tree_policy": self.dirty_tree_policy.value,
            "endpoint_policy": self.endpoint_policy,
            "environment_allowlist": list(self.environment_allowlist),
            "repository_host": self.repository_host,
            "repository_id": self.repository_id,
            "scope": list(self.scope),
            "skill_contract_sha256": self.skill_contract_sha256,
            "source_selection": self.source_selection.value,
            "source_selector": self.source_selector,
            "timeout_seconds": self.timeout_seconds,
            "verification_contract_sha256": self.verification_contract_sha256,
        }

    @property
    def revision_sha256(self) -> str:
        return _sha256(self.revision_payload())
