"""Query evaluation over name indexes (and over ad-hoc row sets such as content hits)."""
import array
import bisect
import heapq
import os
import re
import time

from . import pinyin
from .common import (F_DIR, F_HIDDEN, F_INPKG, F_NOISE, F_PACKAGE, F_USER, HOME, KIND_ZH, abbreviate,
                     ext_of, kind_of)

BASE = (0, 400, 600, 800, 1000)
_BOUNDARY = set(" -_.([+,")


def quality(lower, orig, needle):
    """0 none, 1 substring, 2 word start, 3 prefix, 4 exact name or exact stem."""
    p = lower.find(needle)
    if p < 0:
        return 0
    n = len(needle)
    if lower == needle:
        return 4
    if p == 0:
        rest = lower[n:]
        if rest.startswith(".") and "." not in rest[1:]:
            return 4
        return 3
    while p >= 0:
        a, b = lower[p - 1], lower[p]
        if a in _BOUNDARY:
            return 2
        if a.isascii() != b.isascii():
            return 2
        if a.isascii() and b.isascii() and a.isalnum() and b.isalnum() and a.isdigit() != b.isdigit():
            return 2
        if p < len(orig) and orig[p - 1].islower() and orig[p].isupper():
            return 2
        p = lower.find(needle, p + 1)
    return 1


def _is_ascii_word(s):
    return s.isascii() and s.replace("_", "").replace("-", "").isalnum()


# ------------------------------------------------------------------ row sets

class RowSet:
    """Ad-hoc rows built from explicit paths (content hits, live checks)."""

    def __init__(self, paths, label="content"):
        self.meta = {"root": "/", "label": label}
        self.names, self.flags, self.kind, self.depth, self.size, self.mtime, self._p = [], [], [], [], [], [], []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            is_dir = os.path.isdir(p)
            name = os.path.basename(p)
            pkg = is_dir and ext_of(name) in ("app", "pages", "numbers", "key", "rtfd", "bundle")
            fl = (F_DIR if is_dir else 0) | (F_PACKAGE if pkg else 0)
            if p.startswith(HOME + "/"):
                fl |= F_USER
            if "/." in p:
                fl |= F_HIDDEN
            self.names.append(name)
            self.flags.append(fl)
            self.kind.append(kind_of(name, is_dir, pkg))
            self.depth.append(p.count("/"))
            self.size.append(0 if is_dir else st.st_size)
            self.mtime.append(st.st_mtime)
            self._p.append(p)
        self.parent = [-1] * len(self.names)
        self.end = list(range(len(self.names)))

    @property
    def count(self):
        return len(self.names)

    @property
    def root(self):
        return "/"

    def lower(self, i):
        return self.names[i].lower()

    def path(self, i):
        return self._p[i]


# ------------------------------------------------------------------ context

class _Ctx:
    def __init__(self, idx, q):
        self.idx = idx
        self.q = q
        self.is_rowset = isinstance(idx, RowSet)
        self.scope_ranges = {}   # scope path -> (start, end) | 'all' | None
        self.anc_ranges = {}     # needle -> merged ranges of folders whose name contains it
        self.py_hits = {}        # needle -> set(rows)
        self.qmaps = {}
        self.now = time.time()

    def qmap(self, needle):
        m = self.qmaps.get(needle)
        if m is None:
            m = self.qmaps[needle] = quality_map(self.idx, needle)
        return m

    # ---- scope
    def scope_range(self, scope):
        if scope in self.scope_ranges:
            return self.scope_ranges[scope]
        idx = self.idx
        root = idx.root.rstrip("/") or "/"
        if self.is_rowset:
            r = ("prefix", scope.rstrip("/") + "/")
        elif root == scope or root.startswith(scope.rstrip("/") + "/") or scope == "/":
            r = "all"
        elif scope.startswith(root.rstrip("/") + "/"):
            row = idx.find_dir(scope)
            r = None if row is None else (row + 1, idx.end[row])
        else:
            r = None
        self.scope_ranges[scope] = r
        return r

    def in_scope(self, row, scope):
        r = self.scope_range(scope)
        if r == "all":
            return True
        if r is None:
            return False
        if r[0] == "prefix":
            return self.idx.path(row).startswith(r[1])
        return r[0] <= row <= r[1]

    # ---- ancestors for !name exclusion
    def ancestor_ranges(self, needle):
        if needle in self.anc_ranges:
            return self.anc_ranges[needle]
        idx = self.idx
        ranges = []
        if not self.is_rowset:
            for row in self.qmap(needle):
                if idx.flags[row] & F_DIR and idx.end[row] > row:
                    ranges.append((row + 1, idx.end[row]))
        ranges.sort()
        merged = []
        for a, b in ranges:
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        starts = [m[0] for m in merged]
        self.anc_ranges[needle] = (starts, merged)
        return self.anc_ranges[needle]

    def under_named(self, row, needle):
        if self.is_rowset:
            parts = self.idx.path(row).lower().split("/")[:-1]
            return any(needle in p for p in parts)
        starts, merged = self.ancestor_ranges(needle)
        k = bisect.bisect_right(starts, row) - 1
        return k >= 0 and merged[k][0] <= row <= merged[k][1]

    def pinyin_rows(self, needle):
        if needle not in self.py_hits:
            self.py_hits[needle] = set(_find_py_rows(self.idx, needle)) if not self.is_rowset else None
        return self.py_hits[needle]


def _find_rows(idx, needle):
    """Rows whose lower-case name contains needle (one hit per row), via str.find on the haystack."""
    hay = idx.hay
    starts = idx.starts
    n = len(starts)
    pos = hay.find(needle)
    out = []
    while pos >= 0:
        row = bisect.bisect_right(starts, pos) - 1
        out.append(row)
        nxt = starts[row + 1] if row + 1 < n else len(hay)
        pos = hay.find(needle, max(nxt, pos + 1))
    return out


def _boundary_at(lower, orig, p):
    a, b = lower[p - 1], lower[p]
    if a in _BOUNDARY or a.isascii() != b.isascii():
        return True
    if a.isascii() and a.isalnum() and b.isalnum() and a.isdigit() != b.isdigit():
        return True
    return p < len(orig) and orig[p - 1].islower() and orig[p].isupper()


def quality_map(idx, needle):
    """{row: (quality, slack)} for every row whose name (or pinyin key) contains needle.

    One left-to-right pass over the name haystack; quality is derived from the hit position
    so that rows are never re-searched individually."""
    hay, starts, names = idx.hay, idx.starts, idx.names
    n, total, nl = len(starts), len(hay), len(needle)
    out = {}
    pos = hay.find(needle)
    br = bisect.bisect_right
    while pos >= 0:
        row = br(starts, pos) - 1
        rs = starts[row]
        re_ = starts[row + 1] - 1 if row + 1 < n else total
        if pos == rs:
            after = pos + nl
            if after == re_ or (hay[after] == "." and hay.find(".", after + 1, re_) < 0):
                q = 4
            else:
                q = 3
        else:
            q = 1
            lower = hay[rs:re_]
            orig = names[row]
            p = pos - rs
            while p >= 0:
                if _boundary_at(lower, orig, p):
                    q = 2
                    break
                p = lower.find(needle, p + 1)
        out[row] = (q, re_ - rs - nl)
        pos = hay.find(needle, re_ + 1)
    text = getattr(idx, "py_text", "")
    if text and _is_ascii_word(needle):
        idx.py_row_at(0)                       # builds idx.py_starts on first use
        pst, prow, tl = idx.py_starts, idx.py_rows, len(text)
        pos = text.find(needle)
        while pos >= 0:
            k = br(pst, pos) - 1
            ls = pst[k]
            le = pst[k + 1] - 1 if k + 1 < len(pst) else tl
            row = prow[k]
            if row not in out:
                q = 4 if (pos == ls and le - ls == nl) else 3 if pos == ls else 1
                out[row] = (q, 0)
            elif pos == ls and out[row][0] < 3:
                out[row] = (4 if le - ls == nl else 3, 0)
            pos = text.find(needle, le + 1)
    for row, names in idx.app_aliases.items():
        match_quality, slack = _alias_quality(names, needle)
        old_quality, old_slack = out.get(row, (0, 0))
        if (match_quality, -slack) > (old_quality, -old_slack):
            out[row] = match_quality, slack
    return out


def _alias_quality(names, needle):
    best = (0, 0)
    for name in names:
        lower = name.lower()
        match = (quality(lower, name, needle), len(lower) - len(needle))
        if match[0] and (match[0], -match[1]) > (best[0], -best[1]):
            best = match
    return best


def _find_py_rows(idx, needle):
    text = getattr(idx, "py_text", "")
    if not text or not _is_ascii_word(needle):
        return []
    out, pos = set(), text.find(needle)
    while pos >= 0:
        k = idx.py_row_at(pos)
        out.add(idx.py_rows[k])
        nl = text.find("\n", pos)
        if nl < 0:
            break
        pos = text.find(needle, nl + 1)
    return out


def _regex_rows(idx, pattern):
    hay = idx.hay
    starts = idx.starts
    out, last = [], -1
    for m in pattern.finditer(hay):
        row = bisect.bisect_right(starts, m.start()) - 1
        if row != last:
            out.append(row)
            last = row
    return out


# ------------------------------------------------------------------ atoms

def _name_quality(ctx, a, row):
    if not ctx.is_rowset:
        return ctx.qmap(a.value).get(row, (0, 0))
    idx = ctx.idx
    low = idx.lower(row)
    q = quality(low, idx.names[row], a.value)
    if q:
        return q, len(low) - len(a.value)
    if _is_ascii_word(a.value) and pinyin.has_han(low):
        hits = ctx.pinyin_rows(a.value)
        if hits is None or row in hits:
            best = 0
            for full, init in pinyin.keys(low):
                best = max(best, quality(full, full, a.value), quality(init, init, a.value))
            if best:
                return best, 0
    return 0, 0


def _match(ctx, a, row):
    """Return (matched, quality, slack) for a positive reading of the atom."""
    idx = ctx.idx
    k = a.kind
    if k == "name":
        q, slack = _name_quality(ctx, a, row)
        if a.negated and a.excludes_ancestors and not q:
            if ctx.under_named(row, a.value):
                return True, 1, 0
        return bool(q), q, slack
    if k == "case":
        return a.value in idx.names[row], 1, 0
    if k == "scope":
        return ctx.in_scope(row, a.value), 0, 0
    if k == "exact":
        return idx.path(row) == a.value, 4, 0
    if k == "path":
        return a.value in idx.path(row).lower(), 0, 0
    if k == "glob":
        target = idx.path(row) if a.on_path else idx.names[row]
        return a.value.match(target) is not None, 0, 0
    if k == "regex":
        target = idx.path(row) if a.on_path else idx.names[row]
        return a.value.search(target) is not None, 0, 0
    if k == "ext":
        return (not idx.flags[row] & F_DIR or idx.flags[row] & F_PACKAGE) and ext_of(idx.names[row]) in a.value, 0, 0
    if k == "kind":
        return idx.kind[row] == a.value, 0, 0
    if k == "files":
        fl = idx.flags[row]
        return (not fl & F_DIR) or bool(fl & F_PACKAGE), 0, 0
    if k == "folders":
        fl = idx.flags[row]
        return bool(fl & F_DIR) and not fl & F_PACKAGE, 0, 0
    if k == "size":
        if idx.flags[row] & F_DIR and not idx.flags[row] & F_PACKAGE:
            return False, 0, 0
        lo, hi = a.value
        s = idx.size[row]
        return s >= lo and (hi is None or s <= hi), 0, 0
    if k == "dm":
        lo, hi = a.value
        return lo <= idx.mtime[row] <= hi, 0, 0
    if k == "fuzzy":
        tight = a.raw == "tight"
        m = _fuzzy_re(a.value, tight).search(idx.lower(row))
        if m is None and tight and pinyin.has_han(idx.lower(row)):
            for full, init in pinyin.keys(idx.lower(row)):
                m = _fuzzy_re(a.value, True).search(init) or _fuzzy_re(a.value, True).search(full)
                if m:
                    break
        if m is None:
            return False, 0, 0
        return True, 1, (m.end() - m.start()) - len(a.value)
    if k == "content":
        return True, 0, 0   # resolved by the content pipeline
    return False, 0, 0


_FUZZY = {}


def _fuzzy_re(s, tight=False):
    """Letters in order. tight=True allows at most 2 characters between them (typo fallback)."""
    k = (s, tight)
    if k not in _FUZZY:
        _FUZZY[k] = re.compile((".{0,2}?" if tight else ".*?").join(re.escape(c) for c in s))
    return _FUZZY[k]


def _eval(ctx, row):
    """None if the row fails, else the best name quality and slack."""
    best_q, best_slack, has_name = 0, 0, False
    for clause in ctx.q.clauses:
        ok = False
        for a in clause:
            m, q, slack = _match(ctx, a, row)
            if a.negated:
                m = not m
            if m:
                ok = True
                if not a.negated and a.kind in ("name", "exact", "fuzzy", "case"):
                    has_name = True
                    if q > best_q or (q == best_q and slack < best_slack):
                        best_q, best_slack = q, slack
                break
        if not ok:
            return None
    return (best_q, best_slack) if has_name else (2, 0)


def _score(ctx, row, q, slack):
    idx = ctx.idx
    s = BASE[q] - 2 * min(max(slack, 0), 64)
    fl = idx.flags[row]
    if fl & F_USER and not fl & (F_NOISE | F_INPKG | F_HIDDEN):
        s += 60
    kd = idx.kind[row]
    if kd == 2:
        s += 150
    elif kd == 1:
        s += 20
    age = ctx.now - idx.mtime[row]
    if age <= 86400:
        s += 60
    elif age <= 604800:
        s += 45
    elif age <= 2592000:
        s += 30
    elif age <= 31536000:
        s += 10
    if fl & F_NOISE:
        s -= 350
    if fl & F_INPKG:
        s -= 300
    if fl & F_HIDDEN:
        s -= 200
    s -= 5 * min(idx.depth[row], 16)
    return s


# ------------------------------------------------------------------ candidates

def _glob_literal(a):
    raw = a.raw.lower()
    if a.on_path:
        raw = raw.rsplit("/", 1)[-1]
    parts = [p for p in re.split(r"[*?\[\]]+", raw) if p]
    return max(parts, key=len) if parts else ""


def _atom_rows(ctx, a):
    """A superset of rows matching a positive atom, or None if it must be checked row by row."""
    idx = ctx.idx
    if ctx.is_rowset:
        return None
    if a.kind == "name" and a.value:
        return ctx.qmap(a.value).keys()
    if a.kind == "case" and a.value:
        return set(_find_rows(idx, a.value.lower()))
    if a.kind == "scope":
        r = ctx.scope_range(a.value)
        if r is None:
            return set()
        if r == "all":
            return None
        return range(r[0], r[1] + 1)
    if a.kind == "glob":
        lit = _glob_literal(a)
        if len(lit) >= 2:
            return set(_find_rows(idx, lit))
        return None
    if a.kind == "ext":
        alt = "|".join(re.escape(e) for e in sorted(a.value))
        return set(_regex_rows(idx, re.compile(r"\.(?:%s)$" % alt, re.M)))
    if a.kind in ("kind", "folders"):
        k = a.value if a.kind == "kind" else 1
        data = idx.kind.tobytes()
        return [m.start() for m in re.finditer(re.escape(bytes([k])), data)]
    if a.kind == "size":
        lo, hi = a.value
        hi = hi if hi is not None else 1 << 62
        return [i for i, v in enumerate(idx.size) if lo <= v <= hi]
    if a.kind == "dm":
        lo, hi = a.value
        return [i for i, v in enumerate(idx.mtime) if lo <= v <= hi]
    if a.kind == "exact":
        parent = os.path.dirname(a.value)
        row = idx.find_dir(parent) if hasattr(idx, "find_dir") else None
        if row is None:
            return set()
        return range(row + 1, idx.end[row] + 1)
    if a.kind == "regex" and not a.on_path and not _LINE_UNSAFE.search(a.value.pattern):
        return set(_regex_rows(idx, re.compile(a.value.pattern, a.value.flags | re.M)))
    if a.kind == "fuzzy" and a.value:
        if a.raw == "tight":
            return None if getattr(idx, "py_text", "") else set(_regex_rows(idx, re.compile(_fuzzy_re(a.value, True).pattern)))
        sub = ".*?".join(re.escape(c) for c in a.value)
        return set(_regex_rows(idx, re.compile("^.*?" + sub, re.M)))
    return None


def _priority(a):
    return {"exact": 0, "scope": 1, "name": 2, "case": 2, "glob": 3, "ext": 4, "regex": 5, "fuzzy": 5, "kind": 6,
            "folders": 7, "size": 8, "dm": 8}.get(a.kind, 9) - (min(len(a.value), 8) / 10 if a.kind == "name" else 0)


def candidates(ctx):
    """Intersect the cheap candidate sets of all positive clauses (smallest first)."""
    idx = ctx.idx
    gens = []
    for clause in ctx.q.clauses:
        if any(a.negated for a in clause):
            continue
        if len(clause) == 1:
            gens.append((_priority(clause[0]), clause))
        elif all(a.kind in ("name", "ext", "glob", "case") for a in clause):
            gens.append((5, clause))
    gens.sort(key=lambda g: g[0])
    sets = []
    for _, clause in gens:
        rows = None
        for a in clause:
            r = _atom_rows(ctx, a)
            if r is None:
                rows = None
                break
            rows = r if rows is None else (set(rows) | set(r))
        if rows is not None:
            if not isinstance(rows, (range, set, list)):
                rows = set(rows)
            sets.append(rows)
            if len(rows) == 0:
                return []
    if not sets:
        return range(idx.count)
    sets.sort(key=len)
    base = sets[0]
    for other in sets[1:]:
        if isinstance(other, range):
            lo, hi = other.start, other.stop
            base = [r for r in base if lo <= r < hi]
        elif len(other) < 50 * max(1, len(base)):
            other = other if isinstance(other, set) else set(other)
            base = [r for r in base if r in other]
        if not base:
            return []
    return sorted(base) if not isinstance(base, range) else base


_LINE_UNSAFE = re.compile(r"\\[sSWDn]|\[\^|\(\?s")


# ------------------------------------------------------------------ public

class Hit:
    __slots__ = ("idx", "row", "score", "fuzzy", "offline", "source", "snippet", "path", "name", "flags",
                 "kind", "size", "mtime")

    def __init__(self, idx, row, score, source="", offline=False, fuzzy=False):
        self.idx, self.row, self.score = idx, row, score
        self.source, self.offline, self.fuzzy, self.snippet = source, offline, fuzzy, ""
        self.path = idx.path(row)
        self.name = idx.names[row]
        self.flags, self.kind = idx.flags[row], idx.kind[row]
        self.size, self.mtime = idx.size[row], idx.mtime[row]

    @property
    def is_dir(self):
        return bool(self.flags & F_DIR) and not self.flags & F_PACKAGE

    def to_dict(self, q=None):
        d = {"name": self.name, "path": self.path, "dir": os.path.dirname(self.path), "kind": KIND_ZH.get(self.kind, "文件"),
             "is_dir": self.is_dir, "size": None if self.is_dir else self.size, "modified": round(self.mtime),
             "score": self.score}
        if self.source:
            d["source"] = self.source
        if self.offline:
            d["offline"] = True
        if self.fuzzy:
            d["fuzzy"] = True
        if self.snippet:
            d["snippet"] = self.snippet
        if q is not None:
            d["highlight"] = highlight_spans(self.name, q)
        return d


def run(idx, q, recent_limit=200):
    """Evaluate q on one index. Returns list of (score, row) and the total count."""
    ctx = _Ctx(idx, q)
    out = []
    if getattr(q, "invalid", False):
        return [], 0
    if q.empty:
        for row in range(1, idx.count):
            fl = idx.flags[row]
            if fl & F_DIR or not fl & F_USER or fl & (F_NOISE | F_HIDDEN | F_INPKG):
                continue
            out.append((idx.mtime[row], row))
        top = heapq.nlargest(recent_limit, out)
        return [(int(s), r) for s, r in top], len(top)
    if not ctx.is_rowset and _names_only(q):
        return _run_names(ctx, q)
    for row in candidates(ctx):
        if row == 0 and not ctx.is_rowset:
            continue
        r = _eval(ctx, row)
        if r is None:
            continue
        out.append((_score(ctx, row, r[0], r[1]), row))
    return out, len(out)


def _names_only(q):
    pos = 0
    for c in q.clauses:
        if len(c) != 1 or c[0].kind != "name" or not c[0].value:
            return False
        pos += not c[0].negated
    return pos > 0


def _contains(ctx, row, needle):
    """Name (or one of its pinyin keys) contains needle."""
    if needle in ctx.idx.lower(row) or any(needle in name.lower() for name in ctx.idx.app_aliases.get(row, ())):
        return True
    py = ctx.pinyin_rows(needle)
    return bool(py) and row in py


def _row_quality(ctx, row, needle):
    idx = ctx.idx
    low = idx.lower(row)
    q, slack = quality(low, idx.names[row], needle), len(low) - len(needle)
    alias_q, alias_slack = _alias_quality(idx.app_aliases.get(row, ()), needle)
    if (alias_q, -alias_slack) > (q, -slack):
        return alias_q, alias_slack
    if q:
        return q, slack
    if pinyin.has_han(low) and _is_ascii_word(needle):
        best = 0
        for f, i in pinyin.keys(low):
            best = max(best, quality(f, f, needle), quality(i, i, needle))
        if best:
            return best, 0
    return 0, 0


def _run_names(ctx, q):
    """Tight loop for the common case: name terms (AND) with optional !exclusions."""
    idx = ctx.idx
    terms = [c[0].value for c in q.clauses if not c[0].negated]
    neg = [c[0].value for c in q.clauses if c[0].negated]
    hay, py = idx.hay, getattr(idx, "py_text", "")
    terms.sort(key=lambda t: hay.count(t) + (py.count(t) if py else 0))
    base, others = ctx.qmap(terms[0]), terms[1:]
    if len(base) > 3000:
        maps = [ctx.qmap(t) for t in others]
        lookup = lambda row, k: maps[k].get(row, (0, 0))
    else:
        lookup = lambda row, k: _row_quality(ctx, row, others[k])
    parent, flags, kind, mtime, depth = idx.parent, idx.flags, idx.kind, idx.mtime, idx.depth
    now = ctx.now
    memo = [{0: _contains(ctx, 0, v)} for v in neg]

    def excluded(row):
        for k, v in enumerate(neg):
            if _contains(ctx, row, v):
                return True
            m = memo[k]
            chain, p = [], parent[row]
            while p not in m:
                chain.append(p)
                p = parent[p]
            hit = m[p]
            for c in reversed(chain):
                hit = hit or _contains(ctx, c, v)
                m[c] = hit
            if hit:
                return True
        return False

    out = []
    append = out.append
    for row, (qq, slack) in base.items():
        if row == 0:
            continue
        if others:
            ok = True
            for k in range(len(others)):
                v = lookup(row, k)
                if not v[0]:
                    ok = False
                    break
                if v[0] > qq or (v[0] == qq and v[1] < slack):
                    qq, slack = v
            if not ok:
                continue
        if neg and excluded(row):
            continue
        sc = BASE[qq] - 2 * (slack if slack < 64 else 64)
        fl = flags[row]
        if fl & 128 and not fl & 52:          # user area, not noise / in package / hidden
            sc += 60
        kd = kind[row]
        if kd == 2:
            sc += 150
        elif kd == 1:
            sc += 20
        age = now - mtime[row]
        sc += 60 if age <= 86400 else 45 if age <= 604800 else 30 if age <= 2592000 else 10 if age <= 31536000 else 0
        if fl & 16:
            sc -= 350
        if fl & 32:
            sc -= 300
        if fl & 4:
            sc -= 200
        d = depth[row]
        sc -= 5 * (d if d < 16 else 16)
        append((sc, row))
    return out, len(out)


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.lower())]


def sort_key(sort):
    """(key, reverse) in each sort's natural direction: names A-Z, newest first, largest first."""
    if sort == "name":
        return (lambda h: _natural(h.name)), False
    if sort == "path":
        return (lambda h: _natural(h.path)), False
    if sort == "modified":
        return (lambda h: h.mtime), True
    if sort == "size":
        return (lambda h: -1 if h.is_dir else h.size), True
    return (lambda h: (-h.score, len(h.name), _natural(h.name))), False


def sort_hits(hits, sort="relevance", flip=False):
    key, rev = sort_key(sort)
    hits.sort(key=key, reverse=rev != flip)
    return hits


def highlight_spans(name, q):
    low = name.lower()
    spans = []
    for lit in q.literals():
        lit = lit.lower()
        if not lit:
            continue
        p = low.find(lit)
        while p >= 0:
            spans.append([p, p + len(lit)])
            p = low.find(lit, p + 1)
    if not spans and pinyin.has_han(low):
        for a in q.atoms():
            if not a.negated and a.kind == "name" and _is_ascii_word(a.value):
                m = pinyin.match_span(low, a.value)
                if m:
                    spans.append([m[0], m[1]])
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def mark(name, spans):
    if not spans:
        return name
    out, last = [], 0
    for a, b in spans:
        out.append(name[last:a]); out.append("【" + name[a:b] + "】"); last = b
    out.append(name[last:])
    return "".join(out)
