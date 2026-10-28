"""The Voice Lab page as one text: serve.html followed by each script it loads from the project folder.

Tests that look for a control or a function use this, so they still find it now that the page script is split.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = re.compile(r'<script[^>]*\ssrc="/([^"]+)"')


def page_source():
    html = (ROOT / "serve.html").read_text(encoding="utf-8")
    scripts = [(ROOT / name).read_text(encoding="utf-8") for name in SCRIPT_SOURCE.findall(html)]
    return "\n".join([html, *scripts])
