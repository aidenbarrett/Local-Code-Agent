"""Real third-party repositories for qualification journeys (#418).

The manifest (``internal/acceptance/corpus.toml``) names each repository by URL and an
exact commit, with its licence, our ``.local-agent.toml`` overlay and seeded fault
patches. Only those files of ours are kept in this repository; the third-party source
is fetched once into ``<runtime root>/qualification-cache/<name>-<commit>/`` and every
journey runs in a disposable local clone of that cache, so the cache is never mutated.

Fetching is explicit and online. A run without a cached copy is UNKNOWN for every
corpus journey, never a silent download and never pass or fail. Every manifest error
fails closed with :class:`CorpusError`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "acceptance" / "corpus.toml"
CACHE_DIR_NAME = "qualification-cache"

# Permissive licences only: a corpus repository is copied and built on user machines.
PERMISSIVE_LICENCES = frozenset({"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0",
                                 "BSL-1.0", "Zlib", "ISC"})
FAULT_KINDS = frozenset({"compile", "test"})
_COMMIT = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_GIT_TIMEOUT_S = 600


class CorpusError(ValueError):
    """The manifest, a cached copy or a fault patch is not usable; nothing ran."""


@dataclass(frozen=True)
class Fault:
    id: str
    patch: Path
    kind: str
    expect: tuple[str, ...]


@dataclass(frozen=True)
class CorpusRepository:
    name: str
    url: str
    commit: str
    licence: str
    overlay: Path
    faults: tuple[Fault, ...]

    def fault(self, fault_id: str) -> Fault:
        for fault in self.faults:
            if fault.id == fault_id:
                return fault
        raise CorpusError(f"{self.name} has no fault {fault_id!r}")

    def fault_of_kind(self, kind: str) -> Fault:
        for fault in self.faults:
            if fault.kind == kind:
                return fault
        raise CorpusError(f"{self.name} has no {kind} fault")


def _text(table: dict[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CorpusError(f"{where}: {key} must be a non-empty string")
    return value


def _relative_file(base: Path, raw: str, where: str) -> Path:
    candidate = (base / raw).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise CorpusError(f"{where}: {raw!r} leaves the acceptance directory")
    if not candidate.is_file():
        raise CorpusError(f"{where}: {raw!r} does not exist")
    return candidate


def _overlay(path: Path, where: str) -> Path:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CorpusError(f"{where}: overlay is not readable TOML: {exc}") from exc
    profiles = parsed.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise CorpusError(f"{where}: overlay defines no build profiles")
    return path


def _fault(base: Path, raw: object, where: str) -> Fault:
    if not isinstance(raw, dict):
        raise CorpusError(f"{where}: a fault must be a table")
    fault_id = _text(raw, "id", where)
    kind = _text(raw, "kind", where)
    if kind not in FAULT_KINDS:
        raise CorpusError(f"{where}: fault {fault_id!r} kind must be one of {sorted(FAULT_KINDS)}")
    expect = raw.get("expect")
    if (not isinstance(expect, list) or not expect
            or not all(isinstance(item, str) and item for item in expect)):
        raise CorpusError(f"{where}: fault {fault_id!r} needs a non-empty expect list")
    patch = _relative_file(base, _text(raw, "patch", where), f"{where}: fault {fault_id!r}")
    return Fault(fault_id, patch, kind, tuple(expect))


def load_manifest(path: Path = MANIFEST) -> dict[str, CorpusRepository]:
    """Parse and validate the corpus manifest; any problem raises CorpusError."""
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CorpusError(f"corpus manifest {path} is not readable TOML: {exc}") from exc
    entries = parsed.get("repository")
    if not isinstance(entries, list) or not entries:
        raise CorpusError(f"corpus manifest {path} lists no repository")
    base = path.parent
    out: dict[str, CorpusRepository] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise CorpusError("corpus manifest: a repository must be a table")
        name = _text(raw, "name", "corpus manifest")
        where = f"corpus {name!r}"
        if not _NAME.fullmatch(name):
            raise CorpusError(f"{where}: name must be lowercase letters, digits, '.', '_' or '-'")
        if name in out:
            raise CorpusError(f"{where}: listed twice")
        commit = _text(raw, "commit", where)
        if not _COMMIT.fullmatch(commit):
            raise CorpusError(f"{where}: commit must be a full 40-hex SHA, got {commit!r}")
        licence = raw.get("licence")
        if not isinstance(licence, str) or not licence:
            raise CorpusError(f"{where}: licence (SPDX) is required")
        if licence not in PERMISSIVE_LICENCES:
            raise CorpusError(f"{where}: licence {licence!r} is not an accepted permissive licence")
        url = _text(raw, "url", where)
        if not url.startswith("https://"):
            raise CorpusError(f"{where}: url must be https")
        overlay = _overlay(_relative_file(base, _text(raw, "overlay", where), where), where)
        faults_raw = raw.get("faults", [])
        if not isinstance(faults_raw, list):
            raise CorpusError(f"{where}: faults must be a list")
        faults = tuple(_fault(base, item, where) for item in faults_raw)
        if len({f.id for f in faults}) != len(faults):
            raise CorpusError(f"{where}: fault ids must be unique")
        out[name] = CorpusRepository(name, url, commit, licence, overlay, faults)
    return out


# ------------------------------------------------------------------ cache


def _git(cwd: Path, *args: str, check: bool = True,
         env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise CorpusError("git is not on PATH")
    done = subprocess.run(  # noqa: S603 - resolved git, fixed argv
        [git, "-c", "core.autocrlf=false", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT_S,
        env={**os.environ, **(env_extra or {})},
    )
    if check and done.returncode != 0:
        raise CorpusError(f"git {' '.join(args)} failed in {cwd}: {done.stderr.strip()[:500]}")
    return done


def cache_root(runtime_root: Path) -> Path:
    return runtime_root / CACHE_DIR_NAME


def cache_path(repo: CorpusRepository, root: Path) -> Path:
    """Where the pinned copy of ``repo`` lives under a qualification cache ``root``."""
    return root / f"{repo.name}-{repo.commit}"


def cached_copy(repo: CorpusRepository, root: Path) -> Path | None:
    """The verified cached copy, or None when it has not been fetched.

    A directory that exists but is not exactly the pinned commit is an error, not a
    miss: it would otherwise be silently replaced or, worse, used.
    """
    path = cache_path(repo, root)
    if not path.exists():
        return None
    head = _git(path, "rev-parse", "HEAD", check=False)
    if head.returncode != 0 or head.stdout.strip() != repo.commit:
        raise CorpusError(f"cached {repo.name} at {path} is not commit {repo.commit}; remove it "
                          "and fetch again")
    return path


def fetch(repo: CorpusRepository, root: Path) -> Path:
    """Clone ``repo`` at its pinned commit into the cache (online). Idempotent."""
    existing = cached_copy(repo, root)
    if existing is not None:
        return existing
    root.mkdir(parents=True, exist_ok=True)
    target = cache_path(repo, root)
    partial = target.with_name(target.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        _git(root, "clone", "--quiet", "--no-checkout", repo.url, str(partial))
        _git(partial, "checkout", "--quiet", "--detach", repo.commit)
        head = _git(partial, "rev-parse", "HEAD").stdout.strip()
        if head != repo.commit:
            raise CorpusError(f"{repo.name}: fetched HEAD {head} is not {repo.commit}")
        # Only a complete, verified copy ever appears under the cache name.
        partial.rename(target)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    return target


def check_faults(repo: CorpusRepository, cached: Path) -> None:
    """Every fault patch must apply cleanly at the pinned commit (fail closed).

    Checked against the commit's own blobs in a private index, never the cache
    worktree, whose bytes depend on the machine's line-ending settings (#438).
    """
    with tempfile.TemporaryDirectory(prefix="lca-corpus-index-") as scratch:
        index = str(Path(scratch) / "index")
        _git(cached, "read-tree", repo.commit, env_extra={"GIT_INDEX_FILE": index})
        for fault in repo.faults:
            done = _git(cached, "apply", "--check", "--cached", str(fault.patch), check=False,
                        env_extra={"GIT_INDEX_FILE": index})
            if done.returncode != 0:
                raise CorpusError(f"{repo.name}: fault {fault.id!r} does not apply at "
                                  f"{repo.commit}: {done.stderr.strip()[:300]}")


def disposable_copy(repo: CorpusRepository, cached: Path, dest: Path, *,
                    fault: Fault | None = None) -> Path:
    """A private clone of the cache with our overlay and an optional fault, committed.

    The overlay and the fault are one baseline commit, so the copy starts clean and
    the cache itself is never touched.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(dest.parent, "clone", "--quiet", "--local", "--no-hardlinks", str(cached), str(dest))
    _git(dest, "checkout", "--quiet", "--detach", repo.commit)
    _git(dest, "switch", "--quiet", "-c", "qualification")
    _git(dest, "remote", "remove", "origin")
    shutil.copyfile(repo.overlay, dest / ".local-agent.toml")
    if fault is not None:
        _git(dest, "apply", str(fault.patch))
    _git(dest, "config", "user.email", "qualification@example.invalid")
    _git(dest, "config", "user.name", "Qualification")
    _git(dest, "config", "core.autocrlf", "false")
    _git(dest, "add", "-A")
    # Upstream .gitignore files may ignore dotfiles (cxxopts ignores ".*"); the overlay
    # must be part of the baseline so candidate worktrees carry the same policy.
    _git(dest, "add", "--force", "--", ".local-agent.toml")
    label = f"qualification baseline: {repo.name}" + (f" with {fault.id}" if fault else "")
    _git(dest, "commit", "--quiet", "--no-verify", "-m", label)
    return dest
