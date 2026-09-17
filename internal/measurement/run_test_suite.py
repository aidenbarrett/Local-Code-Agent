"""A very small pytest-compatible runner.

Real pytest is the dependency. This exists because work machines and air-gapped
boxes do not always have a package index, and a test suite you cannot run is
not a test suite. It supports exactly the features this repository's tests use:
function discovery, fixtures (including fixtures depending on fixtures and
generator fixtures), `tmp_path`, `monkeypatch`, `pytest.raises`,
`pytest.mark.parametrize`, `pytest.mark.skipif` and module-level `pytestmark`.

    python measurement/run_test_suite.py tests
    python measurement/run_test_suite.py tests/unit -k policy
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import re
import shutil
import sys
import tempfile
import traceback
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# the `pytest` shim
# --------------------------------------------------------------------------


class _Skipped(Exception):
    pass


class _Failed(Exception):
    pass


class _MarkDecorator:
    def __init__(self, name: str, args: tuple, kwargs: dict) -> None:
        self.name = name
        self.args = args
        self.kwargs = kwargs

    def __call__(self, fn: Callable) -> Callable:
        marks = getattr(fn, "_run_test_suite_marks", [])
        fn._run_test_suite_marks = [*marks, self]  # type: ignore[attr-defined]
        return fn


class _MarkFactory:
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


class _RaisesContext:
    def __init__(self, expected: type[BaseException], match: str | None) -> None:
        self.expected = expected
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self) -> "_RaisesContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise _Failed(f"DID NOT RAISE {self.expected.__name__}")
        if not issubclass(exc_type, self.expected):
            return False
        if self.match and not re.search(self.match, str(exc)):
            raise _Failed(f"{exc!r} does not match {self.match!r}")
        self.value = exc
        return True


def _raises(expected: type[BaseException], match: str | None = None) -> _RaisesContext:
    return _RaisesContext(expected, match)


def _skip(reason: str = "") -> None:
    raise _Skipped(reason)


def _install_shim() -> types.ModuleType:
    module = types.ModuleType("pytest")
    module.fixture = _fixture
    module.mark = _MarkFactory()
    module.raises = _raises
    module.skip = _skip
    module.Skipped = _Skipped
    module.approx = lambda value, rel=1e-6: value
    sys.modules["pytest"] = module
    return module


# --------------------------------------------------------------------------
# built-in fixtures
# --------------------------------------------------------------------------


class MonkeyPatch:
    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def setattr(self, target: Any, name: str, value: Any) -> None:
        old = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, old))
        setattr(target, name, value)

    def setenv(self, name: str, value: str) -> None:
        old = os.environ.get(name)
        self._undo.append(
            lambda: os.environ.pop(name, None) if old is None else os.environ.__setitem__(name, old)
        )
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        old = os.environ[name]
        self._undo.append(lambda: os.environ.__setitem__(name, old))
        del os.environ[name]

    def undo(self) -> None:
        for fn in reversed(self._undo):
            fn()
        self._undo.clear()


# --------------------------------------------------------------------------
# collection and execution
# --------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _collect_fixtures(module: types.ModuleType) -> dict[str, Callable]:
    return {
        name: obj
        for name, obj in vars(module).items()
        if callable(obj) and getattr(obj, "_run_test_suite_fixture", False)
    }


@contextmanager
def _resolve(name: str, fixtures: dict[str, Callable], cache: dict[str, Any]):
    if name in cache:
        yield cache[name]
        return
    if name == "tmp_path":
        path = Path(tempfile.mkdtemp(prefix="run_test_suite-"))
        cache[name] = path
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
        return
    if name == "monkeypatch":
        mp = MonkeyPatch()
        cache[name] = mp
        try:
            yield mp
        finally:
            mp.undo()
        return
    if name not in fixtures:
        raise _Failed(f"unknown fixture {name!r}")

    fn = fixtures[name]
    params = list(inspect.signature(fn).parameters)
    with _resolve_many(params, fixtures, cache) as kwargs:
        if inspect.isgeneratorfunction(fn):
            gen = fn(**kwargs)
            value = next(gen)
            cache[name] = value
            try:
                yield value
            finally:
                for _ in gen:
                    pass
        else:
            value = fn(**kwargs)
            cache[name] = value
            yield value


@contextmanager
def _resolve_many(names: list[str], fixtures: dict[str, Callable], cache: dict[str, Any]):
    if not names:
        yield {}
        return
    head, *rest = names
    with _resolve(head, fixtures, cache) as value:
        with _resolve_many(rest, fixtures, cache) as others:
            yield {head: value, **others}


def _marks_of(fn: Callable, module: types.ModuleType) -> list[_MarkDecorator]:
    marks = list(getattr(fn, "_run_test_suite_marks", []))
    module_marks = getattr(module, "pytestmark", [])
    if isinstance(module_marks, _MarkDecorator):
        module_marks = [module_marks]
    return [*module_marks, *marks]


def _parametrize_cases(marks: list[_MarkDecorator]) -> list[tuple[str, dict[str, Any]]]:
    for mark in marks:
        if mark.name != "parametrize":
            continue
        names = [n.strip() for n in mark.args[0].split(",")]
        cases = []
        for value in mark.args[1]:
            values = value if isinstance(value, tuple) else (value,)
            cases.append(("-".join(map(str, values)), dict(zip(names, values))))
        return cases
    return [("", {})]


def _skip_reason(marks: list[_MarkDecorator]) -> str | None:
    for mark in marks:
        if mark.name == "skipif":
            condition = mark.args[0] if mark.args else mark.kwargs.get("condition")
            if condition:
                return str(mark.kwargs.get("reason", "skipif"))
        if mark.name == "skip":
            return str(mark.kwargs.get("reason", "skip"))
    return None


def run(paths: list[Path], keyword: str | None, verbose: bool) -> int:
    _install_shim()
    root = Path.cwd()
    sys.path.insert(0, str(root / "src"))

    files: list[Path] = []
    for path in (p.resolve() for p in paths):
        if path.is_file():
            files.append(path)
        else:
            files.extend(sorted(path.rglob("test_*.py")))

    conftest_fixtures: dict[str, Callable] = {}
    for conftest in sorted({f.parent for f in files} | {root / "tests"}):
        candidate = conftest / "conftest.py"
        if candidate.is_file():
            module = _load_module(candidate, f"conftest_{candidate.parent.name}")
            conftest_fixtures.update(_collect_fixtures(module))

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []

    for file in files:
        module = _load_module(file, f"run_test_suite_{file.stem}_{id(file)}")
        fixtures = {**conftest_fixtures, **_collect_fixtures(module)}
        tests = [
            (name, obj)
            for name, obj in vars(module).items()
            if name.startswith("test_") and callable(obj)
        ]
        for name, fn in tests:
            marks = _marks_of(fn, module)
            for case_id, params in _parametrize_cases(marks):
                label = f"{file.relative_to(root)}::{name}" + (f"[{case_id}]" if case_id else "")
                if keyword and keyword not in label:
                    continue
                reason = _skip_reason(marks)
                if reason:
                    skipped += 1
                    if verbose:
                        print(f"SKIP {label} ({reason})")
                    continue

                needed = [
                    p for p in inspect.signature(fn).parameters if p not in params
                ]
                try:
                    with _resolve_many(needed, fixtures, {}) as kwargs:
                        fn(**params, **kwargs)
                    passed += 1
                    print(f"  ok  {label}" if verbose else ".", end="" if not verbose else "\n")
                except _Skipped as exc:
                    skipped += 1
                    print(f"SKIP {label} ({exc})" if verbose else "s", end="" if not verbose else "\n")
                except Exception:
                    failed += 1
                    failures.append((label, traceback.format_exc()))
                    print(f"FAIL {label}" if verbose else "F", end="" if not verbose else "\n")
                sys.stdout.flush()

    if not verbose:
        print()
    for label, tb in failures:
        print(f"\n{'=' * 70}\nFAILED {label}\n{'-' * 70}\n{tb}")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=["tests"])
    parser.add_argument("-k", dest="keyword")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return run([Path(p) for p in (args.paths or ["tests"])], args.keyword, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
