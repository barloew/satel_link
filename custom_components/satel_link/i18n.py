"""Satel Link — render dynamic findings to a localized string.

The config-flow framework localizes *static* step text (titles, field labels)
from translations/<lang>.json. But a link's findings are a dynamic list, which
the framework cannot render, so we render them ourselves into a single
placeholder string, using the same bundled translation files.

Keys map to sections in translations/<lang>.json:

    Finding.key       -> "findings"
    LinkCheck.key     -> "checks"
    LinkCheck.remedy_key -> "remedies"

English is the fallback for any missing key or language.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent / "translations"
_FALLBACK = "en"


@lru_cache(maxsize=8)
def _load(language: str) -> dict:
    path = _DIR / f"{language}.json"
    if not path.exists():
        path = _DIR / f"{_FALLBACK}.json"
    return json.loads(path.read_text("utf-8"))


def translate(section: str, key: str, language: str, **placeholders) -> str:
    """Look up section.key for the language, fall back to English, then to the
    bare key. Placeholders are formatted in; a missing one degrades gracefully."""
    for lang in (language, _FALLBACK):
        table = _load(lang).get(section, {})
        if key in table:
            text = table[key]
            try:
                return text.format(**placeholders)
            except (KeyError, IndexError):
                return text
    return key


def render_findings(findings, language: str) -> str:
    """One localized bullet per finding, ordered error -> warning -> info."""
    order = {"error": 0, "warning": 1, "info": 2}
    marker = {"error": "\u2716", "warning": "\u26a0", "info": "\u2139"}
    lines = []
    for finding in sorted(findings, key=lambda f: order.get(f.severity.value, 9)):
        text = translate(
            "findings", finding.key, language, **(finding.placeholders or {})
        )
        lines.append(f"{marker.get(finding.severity.value, '')} {text}")
    return "\n".join(lines)


def render_check(check, language: str) -> str:
    """Localized line for a LinkCheck, with its remedy appended when present."""
    text = translate("checks", check.key, language, **(check.placeholders or {}))
    if check.remedy_key:
        remedy = translate(
            "remedies", check.remedy_key, language, **(check.placeholders or {})
        )
        return f"{text}\n{remedy}"
    return text
