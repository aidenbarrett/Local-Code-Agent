from __future__ import annotations

from devtools.check_test_import_boundaries import find_violations, violations_in_file


def test_internal_package_import_is_rejected(tmp_path):
    test_file = tmp_path / "test_bad.py"
    test_file.write_text(
        "from internal.devtools.some_tool import main\nimport internal.local_agent\n",
        encoding="utf-8",
    )
    violations = violations_in_file(test_file)
    assert [(item.line, item.module) for item in violations] == [
        (1, "internal.devtools.some_tool"),
        (2, "internal.local_agent"),
    ]


def test_imports_from_internal_root_are_allowed(tmp_path):
    test_file = tmp_path / "test_good.py"
    test_file.write_text(
        "from devtools.some_tool import main\nfrom local_agent.session import contracts\n",
        encoding="utf-8",
    )
    assert violations_in_file(test_file) == ()


def test_tree_scan_only_collects_test_modules(tmp_path):
    (tmp_path / "test_bad.py").write_text("import internal.foo\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("import internal.bar\n", encoding="utf-8")
    violations = find_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].module == "internal.foo"
