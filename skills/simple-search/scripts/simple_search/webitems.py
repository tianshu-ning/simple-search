"""匹配自定义网址，并从 Chromium 系浏览器读取和搜索书签。"""
import json
import os
import re
import time
import urllib.parse

from . import pinyin
from .common import HOME
from .search import _is_ascii_word, quality

BROWSERS = [  # (label, folder under ~/Library/Application Support, bundle id)
    ("Chrome", "Google/Chrome", "com.google.Chrome"),
    ("Chrome Beta", "Google/Chrome Beta", "com.google.Chrome.beta"),
    ("Edge", "Microsoft Edge", "com.microsoft.edgemac"),
    ("Brave", "BraveSoftware/Brave-Browser", "com.brave.Browser"),
    ("Chromium", "Chromium", "org.chromium.Chromium"),
]
ROOT_ZH = {"bookmark_bar": "书签栏", "other": "其他书签", "synced": "移动设备书签"}
QUERY_PLACEHOLDER = "{query}"


def load_web_shortcuts(cfg):
    """忽略手动编辑配置时写入的无效条目，重复关键词保留第一条有效记录。"""
    entries = cfg.get("web_shortcuts")
    shortcuts = {}
    if not isinstance(entries, list):
        return shortcuts
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fields = [entry.get(key) for key in ("title", "keyword", "url")]
        if not all(isinstance(field, str) for field in fields):
            continue
        title, keyword, url = (field.strip() for field in fields)
        if not title or len(keyword.split()) != 1 or not _valid_web_shortcut_url(url):
            continue
        shortcuts.setdefault(keyword.lower(), (title, keyword, url))
    return shortcuts


def _valid_web_shortcut_url(url):
    if re.search(r"[\s\x00-\x1f\x7f\\]|%(?![0-9a-fA-F]{2})", url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        # 只允许在路径、查询参数和片段中使用占位符，确保查询词不会改变目标网站。
        tail = "".join(part.replace(QUERY_PLACEHOLDER, "") for part in (parts.path, parts.query, parts.fragment))
        return (parts.scheme in ("http", "https") and bool(parts.hostname)
                and parts.username is None and parts.port != 0
                and not re.search(r"[{}]", parts.netloc + tail))
    except ValueError:
        return False


def web_shortcut_items(text, shortcuts, *, list_all=False):
    words = text.split(maxsplit=1)
    if not words:
        return [_web_shortcut_item(shortcuts[keyword]) for keyword in sorted(shortcuts)] if list_all else []
    prefix = words[0].lower()
    if len(words) == 2:
        shortcut = shortcuts.get(prefix)
        return [_web_shortcut_item(shortcut, words[1])] if shortcut is not None else []
    # 完整关键词是排序后同一前缀中最短的一项，因此会排在首位。
    matches = sorted(keyword for keyword in shortcuts if keyword.startswith(prefix))
    return [_web_shortcut_item(shortcuts[keyword]) for keyword in matches]


def _web_shortcut_item(shortcut, query=""):
    title, keyword, url = shortcut
    if QUERY_PLACEHOLDER in url:
        if not query:
            return {"t": "web_shortcut", "name": title, "path": "", "r1": keyword, "r2": "自定义网址",
                    "sub": f"请输入搜索词，例如：{keyword} 搜索词"}
        url = url.replace(QUERY_PLACEHOLDER, urllib.parse.quote(query, safe=""))
    return {"t": "web_shortcut", "name": title, "path": url, "r1": keyword, "r2": "自定义网址", "sub": url}


def _webkit_time(v):
    """Chromium stores microseconds since 1601-01-01."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0.0
    return n / 1e6 - 11644473600 if n > 0 else 0.0


# ------------------------------------------------------------------ text matching

def _keys(text):
    low = text.lower()
    out = [low]
    if pinyin.has_han(low):
        for full, init in pinyin.keys(low):
            out += [full, init]
    return out


def _title_quality(title, term):
    low = title.lower()
    q = quality(low, title, term)
    if q or not (_is_ascii_word(term) and pinyin.has_han(low)):
        return q
    best = 0
    for full, init in pinyin.keys(low):
        best = max(best, quality(full, full, term), quality(init, init, term))
    return best


def parse_terms(text):
    terms, negs = [], []
    for tok in text.split():
        if tok.startswith("!") and len(tok) > 1:
            negs.append(tok[1:].lower())
        elif tok:
            terms.append(tok.lower())
    return terms, negs


def _age_bonus(ts, now):
    age = now - (ts or 0)
    return 60 if age < 86400 else 45 if age < 604800 else 30 if age < 2592000 else 10 if age < 31536000 else 0


# ------------------------------------------------------------------ bookmarks

_BM_CACHE = {"stamp": None, "items": []}


def bookmark_files(cfg=None):
    base = os.path.join(HOME, "Library", "Application Support")
    out = []
    for label, folder, bundle in BROWSERS:
        root = os.path.join(base, folder)
        if not os.path.isdir(root):
            continue
        names = {}
        try:
            with open(os.path.join(root, "Local State"), encoding="utf-8") as f:
                cache = json.load(f).get("profile", {}).get("info_cache", {})
            names = {k: (v.get("name") or k) for k, v in cache.items()}
        except (OSError, ValueError, AttributeError):
            pass
        for prof in sorted(os.listdir(root)):
            if prof != "Default" and not prof.startswith("Profile "):
                continue
            # 本地书签优先读取，重复链接继续保留原有条目。
            for filename in ("Bookmarks", "AccountBookmarks"):
                p = os.path.join(root, prof, filename)
                if os.path.isfile(p):
                    out.append({"browser": label, "bundle": bundle, "profile": names.get(prof, prof), "path": p})
    extra = ((cfg or {}).get("bookmarks") or {}).get("files") or []
    for p in extra:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            out.append({"browser": "Chrome", "bundle": "com.google.Chrome", "profile": "", "path": p})
    return out


def load_bookmarks(cfg=None):
    files = bookmark_files(cfg)
    stamp = []
    for f in files:
        try:
            stamp.append((f["path"], os.path.getmtime(f["path"])))
        except OSError:
            pass
    if stamp == _BM_CACHE["stamp"]:
        return _BM_CACHE["items"]
    items, seen = [], set()
    multi = len(files) > 1
    for f in files:
        try:
            with open(f["path"], encoding="utf-8") as fh:
                roots = json.load(fh).get("roots", {})
        except (OSError, ValueError):
            continue

        def walk(node, trail):
            if node.get("type") == "url":
                url = node.get("url", "")
                key = (f["browser"], f["profile"], url)
                if url and key not in seen and not url.startswith("javascript:"):
                    seen.add(key)
                    folder = " / ".join(trail)
                    if multi:
                        folder = f"{f['browser']}·{f['profile']}  " + folder
                    items.append({"title": node.get("name") or url, "url": url, "folder": folder,
                                  "added": _webkit_time(node.get("date_added")), "bundle": f["bundle"],
                                  "browser": f["browser"]})
            for child in node.get("children", []) or []:
                walk(child, trail + ([node.get("name")] if node.get("type") == "folder" and trail is not None else []))

        for rk, rv in roots.items():
            if isinstance(rv, dict):
                name = ROOT_ZH.get(rk, rv.get("name") or rk)
                for child in rv.get("children", []) or []:
                    walk(child, [name])
    _BM_CACHE["stamp"], _BM_CACHE["items"] = stamp, items
    return items


def search_bookmarks(text, cfg=None, limit=50):
    items = load_bookmarks(cfg)
    terms, negs = parse_terms(text)
    now = time.time()
    scored = []
    for b in items:
        if not terms:
            scored.append((b["added"], b, 0))
            continue
        hay_url = b["url"].lower()
        hay_folder = " ".join(_keys(b["folder"]))
        if any(n in b["title"].lower() or n in hay_url or n in hay_folder for n in negs):
            continue
        total, ok = 0, True
        for t in terms:
            q = _title_quality(b["title"], t)
            if q:
                total += (0, 400, 600, 800, 1000)[q]
            elif t in hay_folder:
                total += 300            # the folder the page was filed under
            elif t in urllib.parse.unquote(hay_url):
                total += 200
            else:
                ok = False
                break
        if ok:
            scored.append((total + _age_bonus(b["added"], now) - min(len(b["title"]), 80), b, total))
    scored.sort(key=lambda x: -x[0])
    return [b for _, b, _ in scored[:limit]], len(scored)
