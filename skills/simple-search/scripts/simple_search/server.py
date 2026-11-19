"""Resident search process for the native window (`simple-search serve`).

Protocol: one JSON object per line on stdin, one per line on stdout.
  {"op": "search", "id": 7, "q": "readme", "tab": "all", "limit": 80}
      -> {"op": "results", "id": 7, "total": 5156, "ms": 0.4, "items": [...], "note": ""}
  {"op": "topic_open", "key": "codex:..."}           -> {"op": "toast", "text": "..."}
  {"op": "topic_copy", "key": "codex:..."}           -> {"op": "copy", "text": "<resume command>"}
  {"op": "refresh"}                                  -> {"op": "toast", "text": "..."}
The index stays in memory, so a query costs only the search itself. Stale indexes are
refreshed by a detached process and swapped in when the new copy lands on disk.
"""
import argparse
import heapq
import json
import os
import select
import sys
import threading
import time
import urllib.parse

from . import cli, indexer, search, topics, webitems
from .common import F_DIR, abbreviate, date_text, load_config, size_text
from .query import parse

TABS = {"bookmarks": "", "all": "", "folder": "folder:", "app": "kind:app", "doc": "kind:doc", "image": "kind:image",
        "code": "kind:code", "video": "kind:video", "audio": "kind:audio"}
OUT = sys.stdout


def emit(obj):
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    OUT.flush()


def u16(s, i):
    return len(s[:i].encode("utf-16-le")) // 2


def spans16(name, spans):
    return [[u16(name, a), u16(name, b) - u16(name, a)] for a, b in spans]


class State:
    def __init__(self):
        self.cfg = load_config()
        self.web_shortcuts = webitems.load_web_shortcuts(self.cfg)
        self.lock = threading.Lock()
        self.indexes = []          # [idx, online, meta_mtime]
        self.last_check = 0.0
        self.last_topic_sync = 0.0
        self.topic_sync_running = False
        self.load()

    # ---------------------------------------------------------------- indexes
    @staticmethod
    def _meta_mtime(root):
        try:
            return os.path.getmtime(os.path.join(indexer.Index.location(root), "meta.json"))
        except OSError:
            return 0.0

    def load(self):
        notes = []
        sel = cli.select_indexes(self.cfg, None, "never", notes, quiet=True)
        out = []
        for idx, online, _scope in sel:
            _ = idx.hay              # build the search haystack now, not on the first keystroke
            search.run(idx, parse("simple-search-warmup"))   # also loads pinyin keys
            out.append([idx, online, self._meta_mtime(idx.root)])
        with self.lock:
            self.indexes = out

    def maintain(self):
        """Every 20 s: start background refreshes for stale indexes, swap in refreshed ones."""
        now = time.time()
        if now - self.last_check < 20:
            return
        self.last_check = now
        limit = self.cfg.get("auto_refresh_minutes", 30) * 60
        for entry in list(self.indexes):
            idx, online, mt = entry
            if online and idx.age() > limit and not indexer.refreshing(idx.root):
                extra = {k: idx.meta[k] for k in ("volume", "label", "uuid", "adhoc") if k in idx.meta}
                indexer.background_refresh(idx.root, extra)
            cur = self._meta_mtime(idx.root)
            if cur and cur != mt and not indexer.refreshing(idx.root):
                threading.Thread(target=self._reload, args=(entry, cur), daemon=True).start()
        if now - self.last_topic_sync > 60 and not self.topic_sync_running:
            self.topic_sync_running = True
            threading.Thread(target=self._sync_topics, daemon=True).start()

    def _reload(self, entry, mtime):
        try:
            fresh = indexer.Index.load(indexer.Index.location(entry[0].root))
            _ = fresh.hay
        except (OSError, ValueError, KeyError):
            return
        with self.lock:
            entry[0], entry[2] = fresh, mtime
            entry[1] = os.path.isdir(fresh.root)

    def _sync_topics(self):
        try:
            topics.sync(self.cfg)
        except Exception:      # noqa: BLE001 - background sync must never kill the server
            pass
        self.last_topic_sync = time.time()
        self.topic_sync_running = False

    # ---------------------------------------------------------------- search
    def search(self, text, tab, limit):
        t0 = time.perf_counter()
        text = text.strip()
        shortcuts = webitems.web_shortcut_items(text, self.web_shortcuts) if tab == "all" and limit > 0 else []
        result = self._search(text, tab, limit, t0)
        if shortcuts:
            result["items"] = (shortcuts + result["items"])[:limit]
            result["total"] += len(shortcuts)
        return result

    def _search(self, text, tab, limit, t0):
        if tab == "topics":
            return self.search_topics(text, limit, t0)
        if tab == "all" and ":" in text and cli.parse_topic_query(text)[1]:
            return self.search_topics(text, limit, t0)
        if tab == "bookmarks":
            items, total = self.bookmark_items(text, limit)
            note = "没有找到浏览器书签" if total == 0 and (not text or not webitems.load_bookmarks(self.cfg)) else ""
            return self._pack(items, total, t0, note, not text)
        if tab == "web_shortcuts":
            items = webitems.web_shortcut_items(text, self.web_shortcuts, list_all=True)
            note = "还没有配置自定义网址" if not self.web_shortcuts else ""
            return self._pack(items[:max(0, limit)], len(items), t0, note, False)
        q = parse((text + " " + TABS.get(tab, "")).strip())
        notes = [f"语法提示：{d}" for d in q.diagnostics]
        if q.has("content"):
            ns = argparse.Namespace(sort="relevance", reverse=False, limit=limit, in_=None, refresh=False,
                                    no_refresh=True, json=True)
            hits, total, _, _ = cli.file_search(q, ns, self.cfg, notes)
        else:
            hits, total = [], 0
            with self.lock:
                entries = list(self.indexes)
            k = max(limit * 2, 60)
            for idx, online, _ in entries:
                scored, n = search.run(idx, q)
                total += n
                label = idx.meta.get("label") if idx.meta.get("volume") else ""
                for score, row in heapq.nlargest(k, scored):
                    hits.append(search.Hit(idx, row, score, source=label, offline=not online))
        hits = cli._dedupe(search.sort_hits(hits, "relevance"))
        hits = cli._verify(hits, self.cfg, notes, limit)[:limit]
        items = [self.file_item(h, q) for h in hits]
        ttotal = 0
        if tab == "all" and len(text) >= 2 and not q.has("content") and ":" not in text:
            for fn, n in ((self.topic_items, 5), (self.bookmark_items, 4)):
                try:
                    extra, cnt = fn(text, n)
                except Exception:  # noqa: BLE001 - an optional source must never break file search
                    extra, cnt = [], 0
                items += extra
                ttotal += cnt
        ms = (time.perf_counter() - t0) * 1000
        return {"total": total + ttotal, "ms": round(ms, 1), "items": items, "note": "；".join(notes[:2]),
                "recent": not text}

    @staticmethod
    def file_item(h, q):
        name = h.name
        if h.kind == 2:
            name = getattr(h.idx, "app_display_names", {}).get(h.row, name)
        sub = abbreviate(os.path.dirname(h.path))
        if h.snippet:
            sub = sub + "  ·  " + h.snippet.replace("【", "").replace("】", "")
        r2 = "文件夹" if h.is_dir else size_text(h.size)
        if h.offline:
            r2 = (h.source or "离线") + " · 离线"
        return {"t": "file", "name": name, "path": h.path, "sub": sub, "r1": date_text(h.mtime), "r2": r2,
                "hl": spans16(name, search.highlight_spans(name, q)), "dir": h.is_dir,
                "off": bool(h.offline), "fuzzy": bool(h.fuzzy)}

    def topic_items(self, text, limit):
        q, providers, project, dm, archived = cli.parse_topic_query(text)
        if not os.path.exists(topics.db_path()):
            return [], 0
        res, total = topics.search(q, providers or None, archived, limit, dm, project)
        out = []
        for t in res:
            spans = []
            low = t["title"].lower()
            for term in q["terms"]:
                p = low.find(term.lower())
                if p >= 0:
                    spans.append([p, p + len(term)])
            sub = t["source"] + ((" · " + abbreviate(t["project"])) if t["project"] else "")
            if t.get("snippet"):
                sub += "  ·  " + t["snippet"].replace("【", "").replace("】", "")
            out.append({"t": "topic", "name": t["title"], "path": "", "sub": sub, "r1": date_text(t["modified"]),
                        "r2": "AI 对话", "hl": spans16(t["title"], spans), "key": t["key"], "provider": t["provider"]})
        return out, total

    @staticmethod
    def _pack(items, total, t0, note, recent):
        return {"total": total, "ms": round((time.perf_counter() - t0) * 1000, 1), "items": items, "note": note,
                "recent": recent}

    @staticmethod
    def _spans(title, text):
        terms, _ = webitems.parse_terms(text)
        low, spans = title.lower(), []
        from . import pinyin
        for t in terms:
            p = low.find(t)
            if p >= 0:
                spans.append((p, p + len(t)))
            elif t.isascii() and pinyin.has_han(low):
                m = pinyin.match_span(low, t)
                if m:
                    spans.append(m)
        spans.sort()
        return spans16(title, spans)

    def bookmark_items(self, text, limit):
        res, total = webitems.search_bookmarks(text, self.cfg, limit)
        out = []
        for b in res:
            host = urllib.parse.urlsplit(b["url"]).netloc
            out.append({"t": "bookmark", "name": b["title"], "path": b["url"], "sub": f"{b['folder']}  ·  {host}",
                        "r1": date_text(b["added"]), "r2": b["browser"] + " 书签", "hl": self._spans(b["title"], text),
                        "bundle": b["bundle"]})
        return out, total

    def search_topics(self, text, limit, t0):
        items, total = self.topic_items(text, limit)
        note = "" if os.path.exists(topics.db_path()) else "还没有 AI 对话索引，稍后会自动同步。"
        return {"total": total, "ms": round((time.perf_counter() - t0) * 1000, 1), "items": items,
                "note": note, "recent": False}


_PENDING = bytearray()


def _read_batch():
    """All complete lines currently available on stdin (blocks until there is at least one)."""
    fd = sys.stdin.fileno()
    while True:
        if b"\n" in _PENDING:
            # Drain whatever else already arrived so a burst of keystrokes becomes one batch.
            while select.select([fd], [], [], 0)[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                _PENDING.extend(chunk)
            cut = _PENDING.rfind(b"\n") + 1
            data = bytes(_PENDING[:cut])
            del _PENDING[:cut]
            return [l.decode("utf-8", "replace") for l in data.split(b"\n") if l.strip()]
        chunk = os.read(fd, 65536)
        if not chunk:
            return None
        _PENDING.extend(chunk)


def main():
    global OUT
    OUT = sys.stdout
    sys.stdout = sys.stderr          # library prints must never corrupt the protocol
    emit({"op": "status", "text": "正在加载索引…"})
    t0 = time.time()
    state = State()
    count = sum(e[0].count for e in state.indexes)
    emit({"op": "ready", "count": count, "seconds": round(time.time() - t0, 2)})
    while True:
        batch = _read_batch()
        if batch is None:
            return 0
        reqs = []
        for line in batch:
            try:
                reqs.append(json.loads(line))
            except ValueError:
                continue
        searches = [r for r in reqs if r.get("op") == "search"]
        for r in reqs:
            if r.get("op") == "search":
                continue
            try:
                handle_action(state, r)
            except Exception as e:  # noqa: BLE001
                emit({"op": "toast", "text": f"操作失败：{e}"})
        if searches:
            r = searches[-1]                  # typing fast: only the latest query matters
            try:
                res = state.search(r.get("q", ""), r.get("tab", "all"), int(r.get("limit", 80)))
            except RuntimeError as e:
                res = {"total": 0, "ms": 0, "items": [], "note": f"搜索出错：{e}"}
            except Exception as e:  # noqa: BLE001
                res = {"total": 0, "ms": 0, "items": [], "note": f"搜索出错：{type(e).__name__}: {e}"}
            res.update({"op": "results", "id": r.get("id")})
            emit(res)
        state.maintain()


def handle_action(state, r):
    from . import actions
    op = r.get("op")
    if op == "reload_web_shortcuts":
        state.web_shortcuts = webitems.load_web_shortcuts(load_config())
    elif op in ("topic_open", "topic_copy", "topic_terminal"):
        t = topics.get(r.get("key", ""))
        if not t:
            emit({"op": "toast", "text": "找不到这个对话"})
            return
        if op == "topic_copy":
            emit({"op": "copy", "text": t.get("resume", ""), "toast": "已拷贝继续会话的命令"})
        elif op == "topic_terminal" and t.get("resume") and not t["resume"].startswith("open "):
            actions.resume_in_terminal(t["resume"])
        elif t.get("open_url"):
            actions.open_path(t["open_url"])
        elif t.get("resume"):
            actions.resume_in_terminal(t["resume"])
    elif op == "refresh":
        for idx, online, _ in state.indexes:
            if online:
                indexer.background_refresh(idx.root)
        threading.Thread(target=state._sync_topics, daemon=True).start()
        emit({"op": "toast", "text": "正在后台更新索引…"})
