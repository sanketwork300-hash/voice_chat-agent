"""Decide, without a model, whether a reply confirms or declines a pending action.

Only a message that is nothing but an affirmation counts as a confirmation.
"Yes, but only from my office" is neither: it leaves the action pending.
"""

import re
from typing import Literal

Reply = Literal["affirm", "decline", "other"]

_AFFIRM = {"yes", "yep", "yeah", "yup", "sure", "ok", "okay", "confirm", "confirmed", "proceed", "approved", "approve"}
_AFFIRM_PHRASES = {"go ahead", "do it", "go for it", "make the change", "sounds good"}
_DECLINE = {"no", "nope", "nah", "cancel", "stop", "dont", "never", "decline"}
_DECLINE_PHRASES = {"do not", "not now", "never mind", "no thanks", "no thank you", "leave it"}
# Words that may accompany a yes or a no without changing its meaning. "not" is
# deliberately absent from the affirmative set: "not sure" must not confirm.
_AFFIRM_FILLER = {"please", "thanks", "thank", "you", "that", "it", "then", "go", "ahead", "do", "lets", "just",
                  "now", "the", "change", "make", "sounds", "good", "for", "alright"}
_DECLINE_FILLER = {"please", "thanks", "thank", "you", "that", "it", "do", "not", "now", "mind", "leave", "i",
                   "want", "to", "this", "the", "change", "make", "just"}


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def _only_from(words: list[str], allowed: set[str]) -> bool:
    return all(word in allowed for word in words)


def classify_reply(text: str) -> Reply:
    normalized = _normalize(text)
    words = normalized.split()
    if not words or len(words) > 8:
        return "other"

    has_decline = bool(_DECLINE & set(words)) or any(p in normalized for p in _DECLINE_PHRASES)
    if has_decline:
        return "decline" if _only_from(words, _DECLINE | _DECLINE_FILLER) else "other"

    has_affirm = bool(_AFFIRM & set(words)) or any(p in normalized for p in _AFFIRM_PHRASES)
    if has_affirm and _only_from(words, _AFFIRM | _AFFIRM_FILLER):
        return "affirm"
    return "other"
