from pathlib import Path

root = Path(__import__('sys').argv[1]).resolve()
p = root / 'tests/unit/test_provenance.py'
text = p.read_text(encoding='utf-8')
p.write_text(text.rstrip() + '\n', encoding='utf-8', newline='\n')
