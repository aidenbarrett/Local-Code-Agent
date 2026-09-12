from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{path}: expected one target, found {text.count(old)}')
    path.write_text(text.replace(old, new), encoding='utf-8', newline='\n')


root = Path(__import__('sys').argv[1]).resolve()

# Tighten the content snapshot produced by the earlier content-bound repair.
p = root / 'local_agent/tools/testing.py'
text = p.read_text(encoding='utf-8')
old = '_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".txt", ".cmake")'
new = '''_SOURCE_SUFFIXES = (
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".txt", ".cmake", ".in", ".inc", ".ipp", ".tpp", ".def", ".s",
    ".asm", ".py", ".sh",
)
_SOURCE_NAMES = frozenset({
    "CMakeLists.txt", "CMakePresets.json", "CMakeUserPresets.json",
    ".local-agent.toml",
})'''
if old not in text:
    raise SystemExit('source suffix declaration moved')
text = text.replace(old, new, 1)
old = '''def _source_hashes(root: Any, build_dir: str) -> dict[str, str]:
    """Hash every build-relevant source file by canonical repository path."""
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        parts = path.parts
        if build_dir in parts or ".git" in parts or ".local-agent" in parts:
            continue
        if path.name in (BUILD_STAMP, PROFILE_STAMP):
            continue
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(out.items()))
'''
new = '''def _source_hashes(root: Any, build_dir: str) -> dict[str, str]:
    """Hash build inputs by canonical repository-relative path.

    Exclusions are repository-relative. An ancestor outside the worktree named
    `build` must not disable the oracle. Symlinks are not followed outside the
    evidence tree.
    """
    out: dict[str, str] = {}
    build_rel = Path(build_dir)
    skipped = {".git", ".local-agent", ".venv", "__pycache__", ".pytest_cache"}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if skipped.intersection(rel.parts):
            continue
        try:
            rel.relative_to(build_rel)
            continue
        except ValueError:
            pass
        if path.name in (BUILD_STAMP, PROFILE_STAMP):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.name not in _SOURCE_NAMES and path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        out[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(out.items()))
'''
if text.count(old) != 1:
    raise SystemExit('content snapshot helper moved')
text = text.replace(old, new)
p.write_text(text, encoding='utf-8', newline='\n')

# Compatibility runner: unsupported manufactured pytest features fail loudly.
p = root / 'measurement/run_test_suite.py'
replace_once(
    p,
    '''class _MarkFactory:
    def __getattr__(self, name: str) -> Callable[..., _MarkDecorator]:
        def make(*args: Any, **kwargs: Any) -> _MarkDecorator:
            return _MarkDecorator(name, args, kwargs)

        return make


def _fixture(*dargs: Any, **dkwargs: Any):
    def wrap(fn: Callable) -> Callable:
        fn._run_test_suite_fixture = True  # type: ignore[attr-defined]
        return fn

    if dargs and callable(dargs[0]):
        return wrap(dargs[0])
    return wrap
''',
    '''class _MarkFactory:
    _SUPPORTED = frozenset({"parametrize", "skip", "skipif"})

    def __getattr__(self, name: str) -> Callable[..., _MarkDecorator]:
        if name not in self._SUPPORTED:
            raise AttributeError(
                f"compatibility runner does not implement pytest.mark.{name}"
            )
        def make(*args: Any, **kwargs: Any) -> _MarkDecorator:
            return _MarkDecorator(name, args, kwargs)
        return make


def _fixture(*dargs: Any, **dkwargs: Any):
    if dkwargs:
        raise TypeError(
            "compatibility runner does not implement pytest.fixture keyword arguments: "
            + ", ".join(sorted(dkwargs))
        )
    if len(dargs) > 1 or (dargs and not callable(dargs[0])):
        raise TypeError("unsupported pytest.fixture invocation")

    def wrap(fn: Callable) -> Callable:
        fn._run_test_suite_fixture = True  # type: ignore[attr-defined]
        return fn

    if dargs:
        return wrap(dargs[0])
    return wrap
''',
)
text = p.read_text(encoding='utf-8')
text = text.replace('    module.approx = lambda value, rel=1e-6: value\n', '')
p.write_text(text, encoding='utf-8', newline='\n')

# Outcome identity: raw-byte semantics and every function that can define task validity.
p = root / 'local_agent/provenance.py'
text = p.read_text(encoding='utf-8')
text = text.replace('        text = path.read_text(encoding="utf-8")\n        tree = ast.parse(text)\n',
                    '        text = path.read_bytes().decode("utf-8")\n        tree = ast.parse(text)\n', 1)
start = text.index('def outcome_contract_sha256()')
end = text.index('\ndef package_identity', start)
func = '''def outcome_contract_sha256() -> str:
    """Fingerprint the byte-exact contract that makes an evaluation result count."""
    task_contracts = _ROOT / "evaluation" / "task_contracts.py"
    oracle = _ROOT / "evaluation" / "oracle.py"
    endpoints = _ROOT / "evaluation" / "endpoints.py"
    evaluator = _ROOT / "evaluation" / "run_evaluation.py"
    function_names = ("prepare", "establish", "_error_row", "run_case", "run_all")
    sources = {name: _function_source(evaluator, name) for name in function_names}
    required = (task_contracts, oracle, endpoints)
    if any(value is None for value in sources.values()) or not all(p.is_file() for p in required):
        return "unavailable-no-source"

    parts: list[tuple[str, bytes]] = [
        ("evaluation/task_contracts.py", task_contracts.read_bytes()),
        ("evaluation/oracle.py", oracle.read_bytes()),
        ("evaluation/endpoints.py", endpoints.read_bytes()),
    ]
    for name in function_names:
        source = sources[name]
        assert source is not None
        parts.append((f"evaluation.run_evaluation.{name}", source.encode("utf-8")))

    digest = hashlib.sha256()
    for label, content in parts:
        digest.update(label.encode())
        digest.update(b"\\0")
        digest.update(content)
        digest.update(b"\\0")
    return digest.hexdigest()
'''
text = text[:start] + func + text[end:]
p.write_text(text, encoding='utf-8', newline='\n')

# Evidence JSON must be byte-stable across hosts.
p = root / 'measurement/capture_run_manifest.py'
text = p.read_text(encoding='utf-8')
text = text.replace(
    '    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")\n',
    '    tmp.write_text(\n        json.dumps(payload, indent=2, sort_keys=True) + "\\n",\n        encoding="utf-8", newline="\\n",\n    )\n',
    1,
)
p.write_text(text, encoding='utf-8', newline='\n')

# Instrument identity is authoritative on native Windows as well as Linux.
p = root / '.github/workflows/tests.yml'
replace_once(
    p,
    '''  integrity:
    name: fixture and instrument integrity
    runs-on: ubuntu-latest
    timeout-minutes: 15
''',
    '''  integrity:
    name: fixture and instrument integrity (${{ matrix.os }})
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    timeout-minutes: 15
''',
)
