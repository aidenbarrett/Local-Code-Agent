"""Build cleanup must never consume user directories (#392)."""
import json
import sys

import pytest

from local_agent.config import ConfigError, load_repo_config
from local_agent.tools import build_registry
from local_agent.tools.tool_primitives import ToolError


@pytest.mark.parametrize('build_dir', ['../outside', '.', '.git', 'src'])
def test_build_refuses_unowned_cleanup_before_any_process(tmp_path, build_dir):
    root = tmp_path / 'repo'
    root.mkdir()
    target = root / build_dir
    target.mkdir(parents=True, exist_ok=True)
    sentinel = target / 'KEEP'
    sentinel.write_bytes(b'user work')
    command = [sys.executable, '-c', 'raise SystemExit(1)']
    (root / '.local-agent.toml').write_text(
        f'[repo]\nbuild_dir={json.dumps(build_dir)}\n'
        f'[profiles.debug]\nbuild={json.dumps(command)}\n', encoding='utf-8',
    )
    ctx = None
    refused = False
    try:
        registry, ctx, _ = build_registry(load_repo_config(root))
        registry.get('build_target').handler()
    except (ConfigError, ToolError):
        refused = True
    assert sentinel.read_bytes() == b'user work'
    assert ctx is None or ctx.processes_started == 0
    assert refused
