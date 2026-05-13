from __future__ import annotations

import re
from functools import lru_cache

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_MORPH = None
_MORPH_FAILED = False


def keyword_tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").casefold().replace("ё", "е"))


def _looks_like_fragment(token: str) -> bool:
    """Короткие аббревиатуры/фрагменты вроде кип, ууг, пуг не лемматизируем."""
    if len(token) <= 3:
        return True
    if any(ch.isdigit() for ch in token):
        return True
    if not re.search(r"[а-я]", token):
        return True
    return False


def _morph():
    global _MORPH, _MORPH_FAILED
    if _MORPH_FAILED:
        return None
    if _MORPH is not None:
        return _MORPH
    try:
        import pymorphy3

        _MORPH = pymorphy3.MorphAnalyzer()
        return _MORPH
    except Exception:
        _MORPH_FAILED = True
        return None


@lru_cache(maxsize=20_000)
def lemmatize_token(token: str) -> str:
    token = token.casefold().replace("ё", "е").strip()
    if not token or _looks_like_fragment(token):
        return token
    morph = _morph()
    if morph is None:
        return token
    try:
        parsed = morph.parse(token)
    except Exception:
        return token
    if not parsed:
        return token
    normal = str(parsed[0].normal_form or "").casefold().replace("ё", "е").strip()
    return normal or token


def lemmatize_tokens(text: str) -> list[str]:
    return [lemmatize_token(token) for token in keyword_tokens(text)]


def contains_token_sequence(text: str, keyword: str, *, lemmatize: bool = False) -> bool:
    haystack = lemmatize_tokens(text) if lemmatize else keyword_tokens(text)
    needle = lemmatize_tokens(keyword) if lemmatize else keyword_tokens(keyword)
    if not haystack or not needle:
        return False
    if len(needle) == 1:
        return needle[0] in set(haystack)
    last_start = len(haystack) - len(needle)
    return any(haystack[i : i + len(needle)] == needle for i in range(last_start + 1))
