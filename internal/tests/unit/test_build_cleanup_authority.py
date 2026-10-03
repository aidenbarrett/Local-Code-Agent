"""Build cleanup must never consume user directories (#392)."""
import json
import sys

import pytest

from local_agent.cli import cmd_doctor
from local_agent.config import ConfigError, load_repo_config
from local_agent.tools import build_registry
from local_agent.tools.process_runner import CancellationProbe
from local_agent.tools.tool_primitives import BlockedError, ToolError


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


class _Stopped(CancellationProbe):
    requested = True


def test_stop_before_cleanup_preserves_owned_build_tree(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    command = [sys.executable, '-c', 'raise SystemExit(1)']
    (root / '.local-agent.toml').write_text(
        f'[profiles.debug]\nbuild={json.dumps(command)}\n', encoding='utf-8',
    )
    build = root / 'build'
    build.mkdir()
    (build / '.local-agent-owned-build-dir').write_text('owned\n')
    sentinel = build / 'KEEP'
    sentinel.write_bytes(b'build output')
    registry, ctx, _ = build_registry(load_repo_config(root), cancellation_probe=_Stopped())
    with pytest.raises(BlockedError):
        registry.get('build_target').handler()
    assert sentinel.read_bytes() == b'build output'
    assert ctx.processes_started == 0


def test_valid_absent_build_tree_is_claimed_and_build_runs(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    command = [
        sys.executable, '-c',
        "from pathlib import Path; assert Path('build/.local-agent-owned-build-dir').is_file()",
    ]
    (root / '.local-agent.toml').write_text(
        f'[profiles.debug]\nbuild={json.dumps(command)}\n', encoding='utf-8',
    )
    registry, ctx, _ = build_registry(load_repo_config(root))
    result = registry.get('build_target').handler()
    assert result.ok
    assert (root / 'build' / '.local-agent-owned-build-dir').is_file()
    assert ctx.processes_started == 1


@pytest.mark.parametrize('field', ['build_dir', 'run_dir'])
@pytest.mark.parametrize('value', [42, '', '.', '.git', '../outside', '/outside', 'C:/outside'])
def test_config_refuses_unsafe_product_directories(tmp_path, field, value):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / '.local-agent.toml').write_text(
        f'[repo]\n{field}={json.dumps(value)}\n'
        '[profiles.debug]\nbuild=["true"]\n', encoding='utf-8',
    )
    with pytest.raises(ConfigError):
        load_repo_config(root)


def test_config_refuses_build_symlink_that_escapes(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'build').symlink_to(outside, target_is_directory=True)
    (root / '.local-agent.toml').write_text(
        '[profiles.debug]\nbuild=["true"]\n', encoding='utf-8',
    )
    with pytest.raises(ConfigError):
        load_repo_config(root)


def test_public_doctor_reports_unsafe_build_path_without_deleting_it(tmp_path, capsys):
    root = tmp_path / 'repo'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'KEEP'
    sentinel.write_bytes(b'user work')
    (root / '.local-agent.toml').write_text(
        '[repo]\nbuild_dir="../outside"\n'
        '[profiles.debug]\nbuild=["false"]\n', encoding='utf-8',
    )
    args = type('Args', (), {'repo': str(root)})()
    assert cmd_doctor(args) == 2
    assert 'config          : FAILED' in capsys.readouterr().out
    assert sentinel.read_bytes() == b'user work'


def test_a_repository_reached_through_a_symlink_still_loads(tmp_path):
    """Containment compares resolved paths: a symlinked or 8.3-named root is not 'outside'."""
    real = tmp_path / 'real'
    real.mkdir()
    (real / '.local-agent.toml').write_text(
        '[repo]\nbuild_dir = "out"\n[profiles.debug]\nbuild = ["true"]\n', encoding='utf-8')
    link = tmp_path / 'link'
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    assert load_repo_config(link).build_dir == 'out'


def test_an_unowned_build_directory_refusal_says_how_to_proceed(tmp_path):
    root = tmp_path / 'repo'
    (root / 'build').mkdir(parents=True)
    (root / 'build' / 'mine.txt').write_text('user data', encoding='utf-8')
    (root / '.local-agent.toml').write_text(
        '[repo]\nbuild_dir = "build"\n[profiles.debug]\nconfigure = ["cmake", "-S", ".", "-B", "build"]\n'
        'build = ["cmake", "--build", "build"]\n', encoding='utf-8')
    registry, ctx, _store = build_registry(load_repo_config(root))
    with pytest.raises(ToolError) as refused:
        registry.get('configure_project').handler()
    message = str(refused.value)
    assert "'build'" in message and 'repo.build_dir' in message
    assert (root / 'build' / 'mine.txt').read_text(encoding='utf-8') == 'user data'
    assert ctx.processes_started == 0
