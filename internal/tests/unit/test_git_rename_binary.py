"""Real Git status preserves rename identity and binary diffs expose no content."""
from __future__ import annotations

import subprocess


def test_status_rename_paths_and_binary_diff_are_lossless(loaded):
    _sandbox, repo, registry, _patches, _skills = loaded
    root = repo.root
    old, new = 'README.md', 'renamed notes.md'
    binary = root / 'sample.bin'
    binary.write_bytes(b'old\x00private-binary-marker\xff')
    subprocess.run(['git', 'add', '--', 'sample.bin'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=a@b.c',
                    'commit', '-qm', 'binary baseline'], cwd=root, check=True)
    subprocess.run(['git', 'mv', '--', old, new], cwd=root, check=True)
    binary.write_bytes(b'new\x00private-binary-marker\xfe')
    before = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root)
    status = registry.get('git_status').handler().data
    rename = next(item for item in status['changed'] if item['staged'] == 'R')
    assert rename == {'path': new, 'original_path': old, 'staged': 'R', 'worktree': ''}
    diff = registry.get('git_diff').handler().data['diff']
    assert 'Binary files a/sample.bin and b/sample.bin differ' in diff
    assert 'private-binary-marker' not in diff
    assert subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root) == before


def test_nul_status_preserves_unusual_names():
    from local_agent.tools.git import _parse_porcelain_v2

    old, new = 'old\tname.txt', 'new\nname.txt'
    output = f'# branch.head main\x002 R. N... 100644 100644 100644 abc def R100 {new}\x00{old}\x00? note\tfile\x00'
    data = _parse_porcelain_v2(output)
    assert data['branch']['head'] == 'main'
    assert data['changed'] == [{'path': new, 'original_path': old, 'staged': 'R', 'worktree': ''}]
    assert data['untracked'] == ['note\tfile']
