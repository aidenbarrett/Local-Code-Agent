"""Repository-root pytest hooks. Loaded for every invocation, before arguments are
parsed, which is what a command-line option needs: a conftest under internal/tests is
only read once pytest has already rejected an option it does not know."""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent / "internal"


def _ci_shards():
    spec = importlib.util.spec_from_file_location(
        "lca_ci_shards", REPO / "devtools" / "ci_shards.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pytest_addoption(parser):
    parser.addoption(
        "--shard", default=None, metavar="K/N",
        help="run only shard K of N: whole test files, duration-balanced (CI)",
    )


def pytest_collection_modifyitems(config, items):
    """Keep only this shard's files. The N shards partition the collected suite."""
    spec = config.getoption("--shard")
    if not spec:
        return
    shards = _ci_shards()
    index, count = shards.parse_shard(spec)
    files = [item.nodeid.split("::", 1)[0] for item in items]
    keep = set(shards.plan(files, shards.load_weights(), count)[index - 1])
    selected = [item for item, name in zip(items, files) if name in keep]
    deselected = [item for item, name in zip(items, files) if name not in keep]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected
