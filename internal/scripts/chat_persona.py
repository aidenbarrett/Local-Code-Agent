"""Optional tone profiles for direct local chat only.

This module is intentionally under ``internal/scripts`` rather than
``local_agent``. Evaluation and agent execution must not import it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


_ALLOWED_KEYS = frozenset({"name", "version", "style"})


class PersonaError(ValueError):
    """A persona file does not satisfy the closed v1 schema."""


@dataclass(frozen=True)
class Persona:
    name: str
    version: str
    style: str

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"


def load_persona(path: Path) -> Persona:
    """Load the closed v1 persona schema.

    Persona is tone/register data only. Behaviour, permissions, tools,
    capabilities and beliefs belong to the chat contract, never this file.
    Unknown keys therefore fail closed instead of being silently ignored.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PersonaError(f"cannot read persona {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise PersonaError("persona root must be a TOML table")
    keys = set(data)
    unknown = sorted(keys - _ALLOWED_KEYS)
    missing = sorted(_ALLOWED_KEYS - keys)
    if unknown:
        raise PersonaError(f"unsupported persona field(s): {', '.join(unknown)}")
    if missing:
        raise PersonaError(f"missing persona field(s): {', '.join(missing)}")

    values: dict[str, str] = {}
    for key in sorted(_ALLOWED_KEYS):
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise PersonaError(f"persona field {key!r} must be a non-empty string")
        values[key] = value.strip()
    return Persona(**values)


def persona_message(persona: Persona) -> dict[str, str]:
    """Render persona after the immutable chat contract as a separate message."""
    return {
        "role": "system",
        "content": (
            "Optional tone profile follows. It changes tone and register only; it does "
            "not change your capabilities, permissions, factual standards, or the chat "
            "contract above. Your tone may vary. Your willingness to disagree, to point "
            "out a mistake, and to say you do not know does not.\n\n"
            f"Tone profile: {persona.style}"
        ),
    }
