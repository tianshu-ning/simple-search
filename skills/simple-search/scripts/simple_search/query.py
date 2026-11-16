"""Query language.

A query is a list of clauses (AND). A clause is a list of atoms (OR, joined by |).
An atom may be negated with a leading !.
"""
import datetime as _dt
import fnmatch
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field

from .common import HOME, KIND_ALIASES, KIND_NAMES


@dataclass
class Atom:
    kind: str                 # name case path scope glob regex ext kind files folders size dm content
    value: object = None
    negated: bool = False
    on_path: bool = False
    excludes_ancestors: bool = False
    raw: str = ""


@dataclass
class Query:
    clauses: list = field(default_factory=list)
    raw: str = ""
    diagnostics: list = field(default_factory=list)
    invalid: bool = False

    @property
    def empty(self):
        return not self.clauses

    def atoms(self):
        for c in self.clauses:
            for a in c:
                yield a

    def has(self, kind):
        return any(a.kind == kind for a in self.atoms())

    def literals(self):
        """Positive literal text used for highlighting."""
        out = []
        for a in self.atoms():
            if a.negated:
                continue
            if a.kind in ("name", "case"):
                out.append(a.value)
            elif a.kind == "glob" and not a.on_path:
                out += [p for p in re.split(r"[*?]+", a.raw.lower()) if p]
        return out


# ---------------------------------------------------------------- paste cleanup

_ESCAPABLE = set(" ()'\"&;[]")


def normalize_paste(text):
    """Accept paths copied from terminals/editors: file:// URLs, quotes, backslash escapes, :line:col."""
    src = text.strip()
    if not ("/" in src or src.startswith("~") or src.startswith("file:")):
        return text
    if len(src) >= 2 and src[0] in "'\"" and src[-1] == src[0] and src[0] not in src[1:-1]:
        src = src[1:-1]
    if src.startswith("file://"):
        rest = src[7:]
        slash = rest.find("/")
        if slash >= 0:
            src = urllib.parse.unquote(rest[slash:])
    out, i = [], 0
    while i < len(src):
        if src[i] == "\\" and i + 1 < len(src) and src[i + 1] in _ESCAPABLE:
            out.append(src[i + 1]); i += 2
        else:
            out.append(src[i]); i += 1
    src = "".join(out)
    m = re.match(r"^(.*?)(?::\d+){1,2}$", src)
    if m and "." in m.group(1).rsplit("/", 1)[-1]:
        src = m.group(1)
    return src


# ---------------------------------------------------------------- values

_UNITS = [("tb", 10**12), ("gb", 10**9), ("mb", 10**6), ("kb", 10**3), ("t", 10**12), ("g", 10**9),
          ("m", 10**6), ("k", 10**3), ("b", 1)]


def _bytes(s):
    t = s.strip().lower()
    mult = 1
    for suf, m in _UNITS:
        if t.endswith(suf):
            t, mult = t[: -len(suf)], m
            break
    try:
        v = float(t)
    except ValueError:
        return None
    if v < 0 or v != v or v == float("inf"):
        return None
    return int(v * mult)


def parse_size(s):
    s = s.strip()
    if s.startswith(">="):
        n = _bytes(s[2:]); return None if n is None else (n, None)
    if s.startswith("<="):
        n = _bytes(s[2:]); return None if n is None else (0, n)
    if s.startswith(">"):
        n = _bytes(s[1:]); return None if n is None else (n + 1, None)
    if s.startswith("<"):
        n = _bytes(s[1:]); return None if n is None or n == 0 else (0, n - 1)
    if ".." in s:
        a, b = s.split("..", 1)
        a, b = _bytes(a) if a else 0, _bytes(b) if b else None
        if a is None or (b is not None and b < a) or (b is None and s.split("..", 1)[1]):
            return None
        return (a, b)
    named = {"empty": (0, 0), "tiny": (0, 10_000), "small": (10_001, 100_000), "medium": (100_001, 1_000_000),
             "large": (1_000_001, 16_000_000), "huge": (16_000_001, 128_000_000), "gigantic": (128_000_001, None)}
    if s.lower() in named:
        return named[s.lower()]
    n = _bytes(s)
    return None if n is None else (n, n)


def _date_part(s):
    parts = s.split("-")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    y = int(parts[0])
    if y < 1970:
        return None
    try:
        if len(parts) == 1:
            a, b = _dt.datetime(y, 1, 1), _dt.datetime(y + 1, 1, 1)
        elif len(parts) == 2:
            m = int(parts[1]); a = _dt.datetime(y, m, 1)
            b = _dt.datetime(y + (m == 12), m % 12 + 1, 1)
        else:
            a = _dt.datetime(y, int(parts[1]), int(parts[2])); b = a + _dt.timedelta(days=1)
    except ValueError:
        return None
    return a.timestamp(), b.timestamp()


def parse_date(s, now=None):
    now = now or time.time()
    t = s.strip().lower()
    today = _dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    day = 86400

    def rng(a, b):
        return (a, b - 1e-6)

    if t in ("today", "今天"):
        return rng(today.timestamp(), (today + _dt.timedelta(days=1)).timestamp())
    if t in ("yesterday", "昨天"):
        return rng((today - _dt.timedelta(days=1)).timestamp(), today.timestamp())
    if t in ("week", "thisweek", "本周"):
        start = today - _dt.timedelta(days=today.weekday())
        return rng(start.timestamp(), (start + _dt.timedelta(days=7)).timestamp())
    if t in ("lastweek", "上周"):
        start = today - _dt.timedelta(days=today.weekday() + 7)
        return rng(start.timestamp(), (start + _dt.timedelta(days=7)).timestamp())
    if t in ("month", "thismonth", "本月"):
        r = _date_part(f"{today.year}-{today.month}"); return rng(*r)
    if t in ("lastmonth", "上月"):
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return rng(*_date_part(f"{y}-{m}"))
    if t in ("year", "thisyear", "今年"):
        return rng(*_date_part(str(today.year)))
    m = re.fullmatch(r"(\d+)(min|h|d|w|m|y)", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        sec = n * {"min": 60, "h": 3600, "d": day, "w": 7 * day, "m": 30 * day, "y": 365 * day}[u]
        if sec > 100 * 365 * day:
            return None
        return (now - sec, now + 1)
    if ".." in t:
        a, b = t.split("..", 1)
        pa, pb = _date_part(a), _date_part(b)
        if pa and pb and pa[0] <= pb[1]:
            return rng(pa[0], pb[1])
        return None
    for op in (">=", "<=", ">", "<"):
        if t.startswith(op):
            p = _date_part(t[len(op):])
            if not p:
                return None
            if op == ">":
                return (p[1], float("inf"))
            if op == ">=":
                return (p[0], float("inf"))
            if op == "<":
                return (0, p[0] - 1e-6)
            return (0, p[1] - 1e-6)
    p = _date_part(t)
    return rng(*p) if p else None


def kind_value(v):
    v = KIND_ALIASES.get(v.lower(), v.lower())
    if v in KIND_NAMES:
        return KIND_NAMES.index(v) + 1
    if v.isdigit() and 1 <= int(v) <= 8:
        return int(v)
    return None


def glob_regex(pattern):
    return re.compile(fnmatch.translate(pattern), re.I | re.S)


# ---------------------------------------------------------------- parsing

def _tokenize(src):
    out, tok, quoted, started = [], [], False, False
    for c in src:
        if c == '"':
            quoted = not quoted; started = True
            continue
        if not quoted and (c.isspace() or c == "|"):
            if started or tok:
                out.append("".join(tok))
            tok, started = [], False
            if c == "|":
                out.append("|")
        else:
            tok.append(c); started = True
    if started or tok:
        out.append("".join(tok))
    return [t for t in out if t != "" or False]


def _expand_path(s):
    s = s.replace("\\ ", " ")
    if s.startswith("~"):
        s = HOME + s[1:]
    return s


def _path_atom(body, neg):
    """A token with / or ~. Existing directories become a fast subtree scope."""
    p = _expand_path(body)
    if p.startswith("/"):
        trimmed = p.rstrip("/") or "/"
        if os.path.isdir(trimmed) and (body.endswith("/") or not neg):
            return Atom("scope", os.path.normpath(trimmed), neg, raw=body)
    return Atom("path", p.lower(), neg, on_path=True, raw=body)


def _matcher(body, neg, now, diags):
    lower = body.lower()
    if ":" in body and not body.startswith("/") and not re.match(r"^[a-zA-Z]:[\\/]", body):
        key, value = body.split(":", 1)
        key = key.lower()
        if key == "ext":
            exts = {e.strip().lstrip(".").lower() for e in re.split(r"[;,]", value) if e.strip()}
            if exts:
                return Atom("ext", exts, neg, raw=body)
            return Atom("files", None, neg, raw=body) if value == "" else None
        if key == "kind" or key == "type":
            k = kind_value(value)
            if k is None:
                diags.append(f"未知类型 kind:{value}（可用：{', '.join(KIND_NAMES)}）")
                return None
            return Atom("kind", k, neg, raw=body)
        if key == "file":
            return [Atom("files", None, neg, raw=body)] + ([Atom("name", value.lower(), neg, raw=value)] if value else [])
        if key == "folder" or key == "dir":
            return [Atom("folders", None, neg, raw=body)] + ([Atom("name", value.lower(), neg, raw=value)] if value else [])
        if key == "size":
            r = parse_size(value)
            if r is None:
                diags.append(f"无法识别的大小 size:{value}（例：size:>10mb、size:1mb..5mb）")
                return None
            return Atom("size", r, neg, raw=body)
        if key in ("dm", "date", "modified"):
            r = parse_date(value, now)
            if r is None:
                diags.append(f"无法识别的日期 dm:{value}（例：dm:today、dm:7d、dm:2026-10、dm:2026-01..2026-03）")
                return None
            return Atom("dm", r, neg, raw=body)
        if key == "path":
            return Atom("path", _expand_path(value).lower(), neg, on_path=True, raw=body)
        if key in ("in", "under", "scope"):
            return _path_atom(value.rstrip("/") + "/", neg)
        if key == "case":
            return Atom("case", value, neg, raw=body)
        if key == "regex" or key == "re":
            try:
                return Atom("regex", re.compile(value, re.I), neg, on_path="/" in value, raw=body)
            except re.error as e:
                diags.append(f"正则表达式无效 regex:{value}（{e}）")
                return None
        if key == "content" or key == "text":
            if value.strip():
                return Atom("content", value.strip(), neg, raw=body)
            return None
        if key == "fuzzy":
            return Atom("fuzzy", value.lower(), neg, raw=body)
        # Unknown prefix: treat literally (e.g. "note:1").
        return Atom("name", lower, neg, raw=body)
    if "*" in lower or "?" in lower:
        on_path = "/" in lower
        pat = _expand_path(body) if on_path else body
        return Atom("glob", glob_regex(pat), neg, on_path=on_path, raw=body)
    if "/" in lower or lower.startswith("~"):
        return _path_atom(body, neg)
    return Atom("name", lower, neg, raw=body)


def parse(text, now=None):
    now = now or time.time()
    src = normalize_paste(text)
    import unicodedata
    src = unicodedata.normalize("NFC", src)
    q = Query(raw=src)
    # A pasted path with spaces that exists on disk is one token.
    if "/" in src and any(c.isspace() for c in src):
        p = _expand_path(src)
        if p.startswith("/") and os.path.exists(p):
            a = _path_atom(src if not os.path.isdir(p) else src.rstrip("/") + "/", False)
            if not os.path.isdir(p):
                a = Atom("exact", os.path.normpath(p), raw=src)
            q.clauses.append([a])
            return q
    tokens = _tokenize(src)
    alts = []
    for i, tok in enumerate(tokens):
        if tok == "|":
            continue
        neg = False
        body = tok
        if body.startswith("!") and len(body) > 1:
            neg, body = True, body[1:]
        if body:
            m = _matcher(body, neg, now, q.diagnostics)
            if isinstance(m, list):
                if len(m) > 1 and not neg:
                    # file:foo / folder:foo -> two AND clauses, unless part of an OR group
                    nxt = i + 1 < len(tokens) and tokens[i + 1] == "|"
                    prv = i > 0 and tokens[i - 1] == "|"
                    if not nxt and not prv:
                        q.clauses.append([m[0]]); m = m[1]
                    else:
                        m = m[1] if len(m) > 1 else m[0]
                else:
                    m = m[-1]
            if m is not None:
                if m.kind == "name" and neg:
                    m.excludes_ancestors = True
                alts.append(m)
        if i + 1 >= len(tokens) or tokens[i + 1] != "|":
            if alts:
                q.clauses.append(alts)
            alts = []
    if alts:
        q.clauses.append(alts)
    if not q.clauses and any(t != "|" for t in tokens):
        if q.diagnostics:
            q.invalid = True      # only invalid filters: match nothing rather than recent files
        else:
            q.clauses = [[Atom("name", src.lower(), raw=src)]]
    return q


SYNTAX = """\
搜索语法（可自由组合，空格 = 且）
  wd、wendang            拼音首字母或全拼（含多音字：cq / zq 都能找到「重庆」）
  foo bar                同时包含 foo 和 bar
  foo | bar              包含 foo 或 bar
  readme !node_modules   排除：名称或任一上级目录含 node_modules
  "my notes"             引号内视为一个整体（可含空格）
  *.png  IMG_????.jpg    通配符，匹配整个名称；含 / 时匹配完整路径
  ~/Desktop/ png         只在某个目录下找（in:~/Desktop 同义）
  path:src/utils         路径里包含 src/utils
  ext:pdf;docx           按扩展名
  kind:image             folder app doc image video audio code archive（也认中文：图片、文档…）
  file:  folder:         只要文件 / 只要文件夹；file:报告 = 名称含「报告」的文件
  size:>10mb  size:1mb..5mb  size:<500k  size:huge
  dm:today  dm:yesterday  dm:week  dm:lastweek  dm:month  dm:7d  dm:12h
  dm:2026-10-01  dm:2026-01..2026-03  dm:>2026-06
  regex:^IMG_\\d+         正则（不区分大小写）
  case:README            区分大小写
  content:发票            搜文件内容（文档、PDF、Office、图片里的文字与画面）
  fuzzy:rdme             模糊匹配（按字母顺序的子序列）
粘贴路径：file:// 链接、带引号或反斜杠转义的路径、末尾带 :行:列 的路径都能直接识别。
"""
