"""services/matching.py. Does a piece of text mention a keyword?

THE RULE
  * Capital letters never matter.
  * A keyword matches if it appears in the text exactly as typed (as before), OR if its words appear in order with only spaces, hyphens or
    underscores between them. So "Wave Body Mist" also finds "Wave Bodymist", "wave body-mist" and "WAVE_BODY_MIST".
  * Only ever MORE matches than the old exact rule, never fewer: the exact test always runs first.
  * Words are not glued together when the joined name is short (under 8 letters), because a short name written as two words
    ("Sun Day", "Pay Day") would otherwise match ordinary words ("Sunday", "payday").
  * A keyword of one word is matched exactly as before.
"""
from __future__ import annotations

import re
from functools import lru_cache

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_GAP = r"[\s\-_]"          # what may sit between the words of a keyword
_GLUE_MIN_LETTERS = 8      # below this, the words must be separated by at least one gap character


@lru_cache(maxsize=4096)
def _loose(keyword_lower: str) -> re.Pattern | None:
    words = _WORD.findall(keyword_lower)
    if len(words) < 2:
        return None
    gap = _GAP + ("{0,2}" if len("".join(words)) >= _GLUE_MIN_LETTERS else "{1,2}")
    return re.compile(gap.join(re.escape(w) for w in words))


def contains(text_lower: str, keyword: str) -> bool:
    """True if the (already lower-cased) text mentions the keyword. A blank keyword matches nothing."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if kw in text_lower:
        return True
    pattern = _loose(kw)
    return bool(pattern and pattern.search(text_lower))
