"""Text clean-up and the title keys that let one work be recognised across sites."""
from __future__ import annotations

import html
import re
import unicodedata


def clean_description(text, limit):
    if not text:
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", str(text))
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\[/?[a-z]+[^\]]*]", "", text)  # [b], [url=…] and similar markup
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;: ") + "…"
    return text or None


_DROP = re.compile(r"[^\w]+", re.UNICODE)
_NOISE = re.compile(
    r"\((?:official|webtoon|manhwa|manhua|manga|novel|colou?red|uncensored|remake|digital|english)[^)]*\)",
    re.IGNORECASE,
)


def title_key(title):
    """A form of a title that survives the ways sites spell the same name differently: case,
    accents, punctuation, spacing, "&" or "and", and a leading "The"."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", str(title))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NOISE.sub(" ", text.casefold())
    text = text.replace("&", " and ").replace("’", "'").replace("'", "")
    text = re.sub(r"^\s*the\s+", "", text)
    key = _DROP.sub("", text).replace("_", "")
    return key if len(key) >= 3 or any(ord(c) > 0x2E80 for c in key) else ""


def slug_title(slug):
    """A readable title from an address slug such as "solo-leveling-2" → "Solo Leveling 2"."""
    slug = re.sub(r"\.html?$", "", slug.strip("/"))
    words = [w for w in re.split(r"[-_]+", slug) if w]
    return " ".join(w if any(c.isdigit() for c in w) else w.capitalize() for w in words)


def unique(values):
    seen, kept = set(), []
    for value in values:
        if not value:
            continue
        value = str(value).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            kept.append(value)
    return kept
