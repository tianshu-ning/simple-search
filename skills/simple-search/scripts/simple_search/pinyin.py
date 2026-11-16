"""Pinyin keys for Chinese names, with polyphonic support.

Every CJK unified ideograph (U+4E00..U+9FFF) maps to up to three readings, the most
common first. A name yields at most MAX_VARIANTS (full, initials) key pairs so that
"cq" and "zq" both find 重庆, while the key table stays small.
"""
import gzip
import itertools
import os
import re

from .common import SKILL_DIR

MAX_VARIANTS = 4
_TABLE = None


def _table():
    global _TABLE
    if _TABLE is None:
        path = os.path.join(SKILL_DIR, "data", "pinyin.txt.gz")
        try:
            with gzip.open(path, "rb") as f:
                rows = f.read().decode("ascii").split("\n")
            _TABLE = [r.split(",") if r else [] for r in rows]
        except OSError:
            _TABLE = []
    return _TABLE


def available():
    return bool(_table())


_HAN = re.compile("[\u4e00-\u9fff]")


def has_han(s):
    return _HAN.search(s) is not None


def keys(lower_name):
    """Return [(full, initials), ...] for a lower-cased name containing Han characters."""
    table = _table()
    if not table:
        return []
    pieces = []      # each piece: list of alternative (full, initial) strings
    for ch in lower_name:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            syl = table[o - 0x4E00]
            if syl:
                pieces.append([(s, s[0]) for s in syl])
                continue
        pieces.append([(ch, ch)])
    # Expand only the first few polyphonic characters to bound the variant count.
    choices, budget = [], MAX_VARIANTS
    for p in pieces:
        if len(p) > 1 and budget >= 2:
            take = min(len(p), budget)
            choices.append(p[:take])
            budget //= take
        else:
            choices.append(p[:1])
    out, seen = [], set()
    for combo in itertools.product(*choices):
        full = "".join(c[0] for c in combo)
        init = "".join(c[1] for c in combo)
        if (full, init) not in seen:
            seen.add((full, init))
            out.append((full, init))
        if len(out) >= MAX_VARIANTS:
            break
    return out


def match_span(lower_name, term):
    """Character range [a, b) of lower_name matched by an ASCII pinyin term (initials or full spelling)."""
    table = _table()
    if not table or not term.isascii():
        return None
    syllables = []
    for ch in lower_name:
        o = ord(ch)
        syl = table[o - 0x4E00] if 0x4E00 <= o <= 0x9FFF else []
        syllables.append(syl or [ch])
    n = len(syllables)
    for start in range(n):
        # initials: one letter per character
        k, i = 0, start
        while i < n and k < len(term) and any(sy[0] == term[k] for sy in syllables[i]):
            k, i = k + 1, i + 1
        if k == len(term):
            return (start, i)
        # full spelling: whole syllables, the last one may be a prefix
        rest, i = term, start
        while i < n and rest:
            hit = None
            for sy in syllables[i]:
                if rest.startswith(sy):
                    hit = sy
                    break
                if sy.startswith(rest):
                    hit = rest
                    break
            if hit is None:
                break
            rest, i = rest[len(hit):], i + 1
        if not rest and i > start:
            return (start, i)
    return None
