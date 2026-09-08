"""Agent Skills: discovery, progressive disclosure, routing.

Three levels of disclosure:

  startup          name + description only, roughly 100 tokens per skill
  activation       the SKILL.md body, kept under about 5000 tokens
  on demand        references/, scripts/, assets/ loaded by explicit request

A skill is an operating procedure, not a personality. If a line of SKILL.md
does not change what the agent does, delete it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n?", re.DOTALL)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "is", "are",
    "use", "when", "with", "this", "that", "it", "its", "my", "me", "i", "please",
    "you", "your", "be", "as", "at", "by", "from", "was", "were", "do", "does",
}


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Minimal YAML subset: scalars, quoted scalars, folded/literal blocks, inline lists."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    body = text[match.end() :]
    data: dict[str, object] = {}
    lines = match.group("body").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()

        if raw in (">", "|", ">-", "|-"):
            collected: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                collected.append(lines[i].strip())
                i += 1
            joined = " ".join(p for p in collected if p) if raw.startswith(">") else "\n".join(collected)
            data[key] = joined.strip()
            continue

        if not raw and i < len(lines) and lines[i].startswith((" ", "\t")):
            # One level of nested mapping, which is all any SKILL.md needs:
            #   verification:
            #     required: true
            nested: dict[str, object] = {}
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                sub = lines[i].strip()
                i += 1
                if not sub or ":" not in sub:
                    continue
                sub_key, _, sub_raw = sub.partition(":")
                value = sub_raw.strip().strip("'\"")
                if value.lower() in ("true", "false"):
                    nested[sub_key.strip()] = value.lower() == "true"
                else:
                    nested[sub_key.strip()] = value
            data[key] = nested
            continue

        if raw.startswith("[") and raw.endswith("]"):
            items = [x.strip().strip("'\"") for x in raw[1:-1].split(",")]
            data[key] = [x for x in items if x]
            continue

        data[key] = raw.strip("'\"")
    return data, body


def _verification_required(meta: dict[str, object]) -> bool:
    """Accept `verification: required` or a nested `verification: {required: true}`."""
    value = meta.get("verification")
    if isinstance(value, dict):
        return bool(value.get("required"))
    if isinstance(value, str):
        return value.strip().lower() in ("required", "true", "yes")
    return False


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    tools: list[str] = field(default_factory=list)
    # Which model tier this procedure needs. A skill that reads and reports can
    # say "cheap" and be served by the small model on the low-power engine.
    tier: str | None = None
    # Whether a cheap-tier failure may be retried on the strong tier.
    escalation: str = "allowed"
    # Whether this procedure's answer is only meaningful with a tool result
    # behind it. When true and the environment prevented that tool from running,
    # the run is BLOCKED rather than failed, and it does not escalate.
    verification_required: bool = False
    references: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)

    @property
    def catalogue_entry(self) -> str:
        return f"- {self.name}: {self.description}"

    def reference(self, name: str) -> str:
        target = (self.path / "references" / name).resolve()
        if self.path.resolve() not in target.parents:
            raise ValueError("reference path escapes the skill directory")
        if not target.is_file():
            raise FileNotFoundError(f"no reference {name!r} in skill {self.name}")
        return target.read_text(encoding="utf-8")


def default_search_path(repo_root: Path, skills_dir: str = ".github/skills") -> list[Path]:
    """Where skills come from, most specific first.

    1. `$LOCAL_AGENT_SKILLS_PATH` entries, if set
    2. the target repository's own skills directory
    3. the skills shipped alongside this package

    A repository-local skill of the same name wins, so a work repo can override
    `build-and-test` without forking anything.
    """
    import os

    paths: list[Path] = []
    for entry in os.environ.get("LOCAL_AGENT_SKILLS_PATH", "").split(os.pathsep):
        if entry.strip():
            paths.append(Path(entry.strip()))
    paths.append(repo_root / skills_dir)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".github" / "skills"
        if candidate.is_dir():
            paths.append(candidate)
            break
    return paths


class SkillLibrary:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {s.name: s for s in skills}

    @classmethod
    def discover_many(cls, roots: list[Path]) -> "SkillLibrary":
        merged: dict[str, Skill] = {}
        for root in roots:
            for name, skill in cls.discover(root)._skills.items():
                merged.setdefault(name, skill)  # earlier paths win
        return cls(list(merged.values()))

    @classmethod
    def discover(cls, root: Path) -> "SkillLibrary":
        skills: list[Skill] = []
        if not root.is_dir():
            return cls(skills)
        for entry in sorted(root.iterdir()):
            md = entry / "SKILL.md"
            if not (entry.is_dir() and md.is_file()):
                continue
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
            name = str(meta.get("name") or entry.name)
            description = str(meta.get("description") or "").strip()
            tools = meta.get("tools")
            skills.append(
                Skill(
                    name=name,
                    description=description,
                    path=entry,
                    body=body.strip(),
                    tools=[str(t) for t in tools] if isinstance(tools, list) else [],
                    tier=str(meta["tier"]).strip() if meta.get("tier") else None,
                    escalation=str(meta.get("escalation") or "allowed").strip(),
                    verification_required=_verification_required(meta),
                    references=sorted(
                        p.name for p in (entry / "references").glob("*.md")
                    ) if (entry / "references").is_dir() else [],
                    scripts=sorted(
                        p.name for p in (entry / "scripts").iterdir()
                    ) if (entry / "scripts").is_dir() else [],
                )
            )
        return cls(skills)

    def __len__(self) -> int:
        return len(self._skills)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def catalogue(self) -> str:
        """Level one disclosure. Cheap enough to send on every request."""
        if not self._skills:
            return "(no skills installed)"
        return "\n".join(s.catalogue_entry for s in self._skills.values())

    def rank(self, task: str, top: int = 3) -> list[tuple[str, float]]:
        """Deterministic first pass so a small model is not the only router."""
        words = {w for w in re.findall(r"[a-z0-9\-]+", task.lower()) if w not in _STOPWORDS}
        scored: list[tuple[str, float]] = []
        for skill in self._skills.values():
            haystack = f"{skill.name} {skill.description}".lower()
            tokens = {
                w for w in re.findall(r"[a-z0-9\-]+", haystack) if w not in _STOPWORDS
            }
            if not tokens:
                continue
            overlap = words & tokens
            score = len(overlap) / (len(words) or 1)
            # A skill whose own name appears in the task is a strong signal.
            if skill.name.lower() in task.lower():
                score += 1.0
            scored.append((skill.name, round(score, 3)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top]

    # A weak top score, or a top score barely ahead of the runner-up, means the
    # keyword router is guessing. Confidently running the wrong procedure is a
    # worse failure than admitting uncertainty, and it gets misattributed to the
    # model afterwards.
    MIN_SCORE = 0.12
    MIN_MARGIN = 0.05

    def route_with_confidence(self, task: str) -> tuple[str | None, float, bool]:
        """Returns (skill, score, confident)."""
        ranked = self.rank(task, top=2)
        if not ranked or ranked[0][1] <= 0.0:
            return None, 0.0, False
        top_name, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        confident = top_score >= self.MIN_SCORE and (top_score - runner_up) >= self.MIN_MARGIN
        return top_name, top_score, confident

    def route(self, task: str, fallback: str | None = None) -> str | None:
        name, _, _ = self.route_with_confidence(task)
        return name or fallback
