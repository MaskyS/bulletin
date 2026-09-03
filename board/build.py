"""Inject data/offers.json into board/template.html.

Writes two files from the same template:
  board.html        the fragment the Claude artifact host wraps in its own document
  docs/index.html   a complete document for GitHub Pages (or any static host)
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
data = (ROOT / "data" / "offers.json").read_text()
json.loads(data)  # fail loudly on a broken file
page = (ROOT / "board" / "template.html").read_text().replace("__DATA__", data.replace("</script>", "<\\/script>"), 1)
(ROOT / "board.html").write_text(page)

full = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n' + page.split("</style>", 1)[0] + "</style>\n</head>\n<body>\n"
        + page.split("</style>", 1)[1] + "\n</body>\n</html>\n")
(ROOT / "docs").mkdir(exist_ok=True)
(ROOT / "docs" / "index.html").write_text(full)
print(f"board.html and docs/index.html written ({len(page.encode()) // 1024} KB)")
