from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_repo_navigation_distinguishes_path_discovery_from_content_search():
    skill = (REPO / "internal" / "skills" / "repo-navigation" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "`list_files` searches **filenames/paths**" in skill
    assert "`search_text` searches **file contents**, not filenames" in skill
    assert "After a zero-match `search_text`" in skill
    assert "Repository setup or build summary" in skill
    assert "Call `repo_info` first" in skill


def test_repo_navigation_stops_when_build_profile_already_answers_question():
    skill = (REPO / "internal" / "skills" / "repo-navigation" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "that is sufficient" in skill
    assert "Finish\n   immediately using that evidence." in skill
    assert "`submit_answer`" not in skill
    assert "Do **not** enumerate\n   source files, tests, or the whole repository" in skill
    assert "Never repeat the same `list_files` query with a larger limit" in skill
    assert "Once the requested question is answered by current evidence" in skill


def test_search_text_tool_contract_says_pattern_is_content_not_filename():
    source = (REPO / "internal" / "local_agent" / "tools" / "search.py").read_text(
        encoding="utf-8"
    )
    assert "Search text CONTENTS in repository files" in source
    assert "not filenames or paths" in source
    assert "Use list_files to discover files by name/extension" in source
    assert "matched against file contents, not filenames" in source
