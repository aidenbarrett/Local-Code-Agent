from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


_CORRECT_TRANSCRIPT_DEREF_PATTERN = (
    r"Path\([^\n)]*['\"]transcript['\"][^\n)]*\)\s*\.\s*"
    r"(?:read_text|read_bytes|open|exists|is_file)\s*\("
)


def main() -> None:
    script = Path(__file__).with_name("item9_final_contract_fix.py")
    spec = importlib.util.spec_from_file_location("item9_final_contract_fix", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    original_compile = re.compile

    def compile_guard(pattern, flags=0):
        if (
            isinstance(pattern, str)
            and "transcript" in pattern
            and "read_text|read_bytes|open|exists|is_file" in pattern
        ):
            pattern = _CORRECT_TRANSCRIPT_DEREF_PATTERN
        return original_compile(pattern, flags)

    module.re.compile = compile_guard
    module.main()


if __name__ == "__main__":
    main()
