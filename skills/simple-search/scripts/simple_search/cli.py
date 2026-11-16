"""simple-search 命令行接口。"""
import argparse
import heapq
import json
import os
import sys
import time

from . import content as content_mod
from . import indexer, pinyin, search, topics
from . import actions
from .common import (HOME, IS_MAC, KIND_ZH, VERSION, abbreviate, age_text, date_text, default_content_roots,
                     default_roots, eprint, expand, load_config, save_config, size_text, which)
from .query import SYNTAX, Atom, _tokenize, parse, parse_date

COMMANDS = {"bookmarks", "ui", "serve", "app", "search", "index", "update", "content", "topics", "open", "reveal", "preview", "copy", "trash", "status",
            "doctor", "syntax", "config", "bench", "help", "-h", "--help", "--version"}


# ------------------------------------------------------------------ index selection

def _covering(metas, path):
    best = None
    for m in metas:
        r = m["root"].rstrip("/") or "/"
        if path == r or path.startswith(r.rstrip("/") + "/") or r == "/":
            if best is None or len(r) > len(best["root"]):
                best = m
    return best


def _load(meta, cfg, refresh, notes, quiet):
    idx = None
    try:
        idx = indexer.Index.load(meta["_folder"])
    except (OSError, ValueError, KeyError):
        idx = None
    age = idx.age() if idx is not None else float("inf")
    stale = age > cfg.get("auto_refresh_minutes", 30) * 60
    online = os.path.isdir(meta["root"])
    extra = {k: meta[k] for k in ("volume", "label", "uuid", "adhoc") if k in meta}
    if online and (refresh == "always" or (refresh == "auto" and stale and (idx is None or age > 7 * 86400))):
        idx = indexer.refresh(meta["root"], cfg, extra_meta=extra, quiet=quiet)
    elif online and refresh == "auto" and stale:
        # Answer now from the current index; refresh quietly for the next search.
        if indexer.background_refresh(meta["root"], extra):
            notes.append(f"{abbreviate(meta['root'])} 的索引更新于 {age_text(age)}，已在后台刷新；要包含刚刚新建的文件可加 --refresh。")
    return idx, online


def select_indexes(cfg, ins, refresh, notes, quiet=False):
    """Returns [(index, online, scope_or_None)]."""
    metas = indexer.list_indexes()
    out = []
    if ins:
        for p in ins:
            p = expand(p)
            if not os.path.exists(p):
                notes.append(f"路径不存在：{p}")
                continue
            m = _covering([m for m in metas if os.path.isdir(m["root"])], p)
            if m is None:
                # Not covered by any index: build a cached ad-hoc index for this folder.
                eprint(f"为 {abbreviate(p)} 建立临时索引（之后会复用并自动更新）…")
                idx = indexer.refresh(p, cfg, extra_meta={"adhoc": True}, quiet=quiet)
                out.append((idx, True, None))
                continue
            idx, online = _load(m, cfg, refresh, notes, quiet)
            if idx is not None:
                out.append((idx, online, p if p != m["root"] else None))
        return out
    roots = default_roots(cfg)
    known = {m["root"]: m for m in metas}
    for r in roots:
        if r not in known:
            eprint(f"首次使用：为 {abbreviate(r)} 建立索引…")
            idx = indexer.refresh(r, cfg, quiet=quiet)
            out.append((idx, True, None))
        else:
            idx, online = _load(known[r], cfg, refresh, notes, quiet)
            if idx is not None:
                out.append((idx, online, None))
    # External drives: mounted ones stay fresh, unplugged ones are still searchable.
    mounted = {v["path"]: v for v in indexer.mounted_volumes()} if cfg.get("index_volumes", True) else {}
    for m in metas:
        if not m.get("volume"):
            continue
        idx, online = _load(m, cfg, refresh if m["root"] in mounted else "never", notes, quiet)
        if idx is not None:
            out.append((idx, online, None))
    missing = [v for p, v in mounted.items() if p not in known]
    if missing:
        notes.append("未建索引的外置磁盘：" + "、".join(v["label"] for v in missing) + "（运行 index --volumes 即可加入）")
    # Drop indexes nested in another selected root (avoid duplicate rows).
    roots_sel = [i.root for i, _, _ in out]
    out = [t for t in out if not any(t[0].root != r and t[0].root.startswith(r.rstrip("/") + "/") for r in roots_sel)]
    return out


# ------------------------------------------------------------------ file search

def _strip_content(q):
    """Split content atoms from the rest of the query."""
    terms, negs, rest = [], [], []
    for clause in q.clauses:
        c = [a for a in clause if a.kind != "content"]
        for a in clause:
            if a.kind == "content":
                (negs if a.negated else terms).extend(content_mod.split_terms(a.value))
        if c and len(c) == len(clause):
            rest.append(c)
    q2 = type(q)(clauses=rest, raw=q.raw, diagnostics=q.diagnostics, invalid=q.invalid)
    return terms, negs, q2


_DOC_EXT = {"md", "markdown", "txt", "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "pages", "numbers", "key",
            "rtf", "odt", "epub", "png", "jpg", "jpeg", "heic", "webp"}


def _content_score(h, hits, now, name_score):
    """Rank content hits: occurrences (log scale), documents over code/data files, recency."""
    import math
    from .common import ext_of
    s = 400 + int(120 * math.log2(1 + min(hits, 200)))
    s += 80 if ext_of(h.name) in _DOC_EXT else -60
    age = now - h.mtime
    s += 60 if age < 86400 else 45 if age < 604800 else 30 if age < 2592000 else 10 if age < 31536000 else 0
    if h.size > 1_000_000 and ext_of(h.name) in ("json", "csv", "log", "js", "html"):
        s -= 120          # big data/bundle files mention everything
    return s + max(0, name_score - 500) // 4


def _top(idx, scored, sort, flip, k):
    if sort == "relevance":
        if flip:
            return heapq.nsmallest(k, scored)
        return heapq.nlargest(k, scored)
    if sort == "modified":
        key = lambda t: idx.mtime[t[1]]
    elif sort == "size":
        key = lambda t: idx.size[t[1]]
    else:
        return scored if len(scored) <= 20000 else scored[:20000]
    rev_large = (sort in ("modified", "size")) != flip
    return heapq.nlargest(k, scored, key=key) if rev_large else heapq.nsmallest(k, scored, key=key)


def file_search(q, args, cfg, notes):
    t_sel = time.time()
    terms, negs, q2 = _strip_content(q)
    sort, flip, limit = args.sort, args.reverse, args.limit
    hits = []
    total = 0
    refresh = "always" if args.refresh else ("never" if args.no_refresh else "auto")
    if terms or negs:
        ins = [expand(p) for p in (args.in_ or [])]
        scope = ins[0] if len(ins) == 1 else None
        t0 = time.time()
        found = content_mod.search(terms, negs, scope=scope) if terms else []
        snippets = {p: s for p, s, _ in found}
        counts = {p: n for p, _, n in found}
        st = content_mod.status()
        use_spot = (cfg.get("content") or {}).get("spotlight", "auto")
        spot = []
        if terms and use_spot in ("auto", True, "on") and IS_MAC:
            spot = [p for p in content_mod.spotlight(terms, scope) if p not in snippets]
        if not st.get("exists") and not spot:
            notes.append("还没有内容索引：运行 content index 建立（文档、PDF、Office、图片文字），之后即可用 content: 搜索。")
        paths = list(snippets) + spot
        if ins and not scope:
            paths = [p for p in paths if any(p == r or p.startswith(r.rstrip("/") + "/") for r in ins)]
        rs = search.RowSet(paths)
        scored, total = search.run(rs, q2) if not q2.empty else ([(500, i) for i in range(rs.count)], rs.count)
        now = time.time()
        for score, row in scored:
            h = search.Hit(rs, row, score, source="内容")
            h.snippet = snippets.get(h.path, "（Spotlight 命中）")
            h.score = _content_score(h, counts.get(h.path, 1), now, score if not q2.empty else 0)
            hits.append(h)
        elapsed = (time.time() - t0) * 1000
        return hits, total, elapsed, []
    selected = select_indexes(cfg, args.in_, refresh, notes, quiet=args.json)
    if not selected:
        notes.append("没有可用的索引。")
        return [], 0, 0, []
    t0 = time.time()
    k = max(limit * 3, 60)
    used = []
    for idx, online, scope in selected:
        qq = q2
        if scope:
            qq = type(q2)(clauses=q2.clauses + [[Atom("scope", scope)]], raw=q2.raw)
        scored, n = search.run(idx, qq)
        total += n
        label = idx.meta.get("label") if idx.meta.get("volume") else ""
        for score, row in _top(idx, scored, sort, flip, k):
            hits.append(search.Hit(idx, row, score, source=label, offline=not online))
        used.append((idx, online))
    # Typo-tolerant fallback when nothing matched.
    if total == 0 and not q2.empty and len(q2.clauses) == 1 and len(q2.clauses[0]) == 1:
        a = q2.clauses[0][0]
        if a.kind == "name" and not a.negated and len(a.value) >= 3:
            fq = type(q2)(clauses=[[Atom("fuzzy", a.value, raw="tight")]], raw=q2.raw)
            for idx, online, scope in selected:
                scored, n = search.run(idx, fq)
                total += n
                for score, row in heapq.nlargest(k, scored):
                    hits.append(search.Hit(idx, row, score - 300, offline=not online, fuzzy=True))
            if total:
                notes.append(f"没有精确匹配「{a.value}」，以下是模糊匹配结果。")
    elapsed = (time.time() - t0) * 1000
    return hits, total, elapsed, used


def _verify(hits, cfg, notes, need):
    if not cfg.get("verify_results", True):
        return hits
    out, gone = [], 0
    for h in hits:
        if len(out) >= need:
            out.append(h)
            continue
        if h.offline:
            out.append(h)
            continue
        try:
            st = os.lstat(h.path)
        except OSError:
            gone += 1
            continue
        h.mtime = st.st_mtime
        if not h.is_dir:
            h.size = st.st_size
        out.append(h)
    if gone:
        notes.append(f"已过滤 {gone} 个刚被删除或移动的条目（下次搜索会自动更新索引）。")
    return out


def _dedupe(hits):
    seen, out = set(), []
    for h in hits:
        if h.path in seen:
            continue
        seen.add(h.path)
        out.append(h)
    return out


def print_files(hits, total, elapsed, q, used, args):
    shown = len(hits)
    info = ""
    if used:
        ages = [i.age() for i, online in used if online]
        if ages:
            info = f" · 索引更新于 {age_text(max(ages))}"
    print(f"文件 · 共 {total:,} 个，显示 {shown} 个 · 用时 {elapsed:.1f} ms{info}")
    if q.empty and not q.invalid:
        print("（空查询：最近修改的文件）")
    for i, h in enumerate(hits, 1):
        name = search.mark(h.name, search.highlight_spans(h.name, q))
        meta = [KIND_ZH.get(h.kind, "文件")]
        if not h.is_dir:
            meta.append(size_text(h.size))
        meta.append(date_text(h.mtime))
        if h.source == "内容":
            meta.append("内容命中")
        elif h.source:
            meta.append("磁盘 " + h.source)
        if h.offline:
            meta.append("离线")
        if h.fuzzy:
            meta.append("模糊")
        print(f"{i:>3}. {name}{'/' if h.is_dir else ''}  [{' · '.join(m for m in meta if m)}]")
        print(f"     {abbreviate(os.path.dirname(h.path))}")
        if h.snippet:
            print(f"     「{h.snippet}」")


# ------------------------------------------------------------------ topic search

def parse_topic_sources(value):
    aliases = {"claude-code": "claude", "cc": "claude", "wb": "workbuddy"}
    providers = []
    for name in value.lower().replace(";", ",").split(","):
        name = name.strip()
        if not name:
            continue
        provider = aliases.get(name, name)
        if provider not in topics.PROVIDERS:
            raise RuntimeError(f"不支持的 AI 对话来源：{name}。可选来源：{', '.join(topics.PROVIDERS)}。")
        providers.append(provider)
    return providers


def parse_topic_query(text):
    q = {"terms": [], "not": []}
    providers, project, dm, archived = [], None, None, False
    for tok in _tokenize(text):
        if tok == "|":
            continue
        low = tok.lower()
        if low.startswith(("source:", "from:", "app:")):
            providers.extend(parse_topic_sources(tok.split(":", 1)[1]))
        elif low.startswith("project:"):
            project = tok.split(":", 1)[1]
        elif low.startswith("dm:"):
            dm = parse_date(tok.split(":", 1)[1])
        elif low in ("archived:yes", "archived:1", "archived:true"):
            archived = True
        elif tok.startswith("!") and len(tok) > 1:
            q["not"].append(tok[1:])
        elif tok:
            q["terms"].append(tok)
    return q, providers, project, dm, archived


def topic_search(text, args, cfg, notes):
    q, providers, project, dm, archived = parse_topic_query(text)
    requested_sources = parse_topic_sources(getattr(args, "source", None) or "")
    providers = providers or requested_sources
    if not getattr(args, "no_sync", False):
        st = topics.sync(cfg, only=providers or None)
        if st.get("errors"):
            for p, e in st["errors"].items():
                notes.append(f"{topics.PROVIDER_ZH.get(p, p)} 同步失败：{e}")
        if not st.get("providers"):
            notes.append("本机没有检测到支持的 AI 对话记录。")
    t0 = time.time()
    res, total = topics.search(q, providers or topics.enabled_providers(cfg), archived or getattr(args, "archived", False),
                               args.limit, dm, project)
    return res, total, (time.time() - t0) * 1000


def print_topics(res, total, elapsed):
    print(f"AI 话题 · 共 {total:,} 个，显示 {len(res)} 个 · 用时 {elapsed:.1f} ms")
    for i, t in enumerate(res, 1):
        meta = [t["source"], date_text(t["modified"])]
        if t["project"]:
            meta.append(abbreviate(t["project"]))
        if t["archived"]:
            meta.append("已归档")
        print(f"{i:>3}. {t['title']}  [{' · '.join(meta)}]")
        if t.get("snippet"):
            print(f"     「{t['snippet']}」")
        if t.get("resume"):
            print(f"     继续：{t['resume']}")
        print(f"     key: {t['key']}")


# ------------------------------------------------------------------ commands

def cmd_search(args, cfg):
    text = " ".join(args.query)
    notes = []
    q = parse(text)
    notes += [f"语法提示：{d}" for d in q.diagnostics]
    payload = {"query": text}
    if args.scope in ("files", "all"):
        hits, total, elapsed, used = file_search(q, args, cfg, notes)
        hits = _dedupe(search.sort_hits(hits, args.sort, args.reverse))
        hits = _verify(hits, cfg, notes, args.limit)[: args.limit]
        if args.json:
            payload["files"] = {"total": total, "elapsed_ms": round(elapsed, 2), "results": [h.to_dict(q) for h in hits]}
        elif args.paths:
            for h in hits:
                print(h.path)
        else:
            print_files(hits, total, elapsed, q, used, args)
    if args.scope in ("topics", "all"):
        res, total, elapsed = topic_search(text, args, cfg, notes)
        if args.json:
            payload["topics"] = {"total": total, "elapsed_ms": round(elapsed, 2), "results": res}
        elif not args.paths:
            if args.scope == "all":
                print()
            print_topics(res, total, elapsed)
    if args.json:
        payload["notes"] = notes
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        for n in notes:
            if args.paths:
                eprint("提示：" + n)
            else:
                print("提示：" + n)
    return 0


def cmd_index(args, cfg):
    if args.list:
        metas = indexer.list_indexes()
        if not metas:
            print("还没有索引。")
        for m in metas:
            tag = "外置磁盘 " + m.get("label", "") if m.get("volume") else ("临时" if m.get("adhoc") else "常规")
            online = "在线" if os.path.isdir(m["root"]) else "离线"
            print(f"- {abbreviate(m['root'])}  [{tag} · {online}] {m.get('count', 0):,} 项 · 更新于 {age_text(time.time() - m.get('updated', 0))}"
                  f" · 扫描 {m.get('scan_seconds', '?')} 秒")
        return 0
    if args.remove:
        for p in args.remove:
            p = expand(p)
            indexer.remove(p)
            if p in [expand(r) for r in cfg.get("roots", [])]:
                cfg["roots"] = [r for r in cfg["roots"] if expand(r) != p]
                save_config(cfg)
            print(f"已移除索引：{abbreviate(p)}")
        return 0
    if args.add:
        roots = cfg.get("roots") or default_roots(cfg)
        for p in args.add:
            p = expand(p)
            if p not in roots:
                roots.append(p)
        cfg["roots"] = roots
        save_config(cfg)
    roots = [expand(r) for r in args.root] if args.root else default_roots(cfg)
    if args.add:
        roots = [expand(p) for p in args.add]
    if args.background:
        try:
            extra = json.loads(args.volume_meta) if args.volume_meta else None
            for r in roots:
                old = indexer.load_for(r)
                meta = extra or ({k: old.meta[k] for k in ("adhoc",) if k in old.meta} if old else None)
                indexer.refresh(r, cfg, extra_meta=meta, quiet=True)
        finally:
            for r in roots:
                try:
                    os.unlink(indexer.lock_path(r))
                except OSError:
                    pass
        return 0
    t0 = time.time()
    for r in roots:
        old = indexer.load_for(r)
        meta = {k: old.meta[k] for k in ("adhoc", "volume", "label", "uuid") if k in old.meta} if old else None
        indexer.refresh(r, cfg, full=args.full, extra_meta=meta)
    if args.cmd == "update":
        for m in indexer.list_indexes():
            if m.get("adhoc") and os.path.isdir(m["root"]) and m["root"] not in roots:
                indexer.refresh(m["root"], cfg, full=args.full, extra_meta={"adhoc": True})
    if args.volumes or (not args.root and not args.add and cfg.get("index_volumes", True)):
        for v in indexer.mounted_volumes():
            indexer.refresh(v["path"], cfg, full=args.full, extra_meta={"volume": True, "label": v["label"], "uuid": v["uuid"]})
    print(f"完成，用时 {time.time() - t0:.1f} 秒。")
    return 0


def cmd_content(args, cfg):
    if args.action == "status":
        st = content_mod.status()
        if not st.get("exists"):
            print("还没有内容索引。运行：content index")
            return 0
        print(f"内容索引：{st['documents']:,} 个文件（无文字 {st['empty']:,}）· {st['size_mb']} MB"
              f" · 更新于 {age_text(time.time() - (st['updated'] or 0))}")
        print("按类型：" + "、".join(f"{k} {v}" for k, v in list(st["by_type"].items())[:15]))
        print("范围：" + "、".join(abbreviate(r) for r in st["roots"]))
        return 0
    if args.action == "index":
        roots = args.root or None
        r = content_mod.build(roots, cfg, images=False if args.no_images else None, max_seconds=args.max_seconds,
                              force=args.force)
        print(f"完成：新增/更新 {r['indexed']:,} 个，移除 {r['removed']:,} 个，用时 {r['seconds']} 秒；"
              f"OCR：{r['ocr'] or '不可用'}。")
        if r["remaining"]:
            print(f"还剩 {r['remaining']:,} 个未处理（达到时间上限），再次运行会接着处理。")
        return 0
    if args.action == "search":
        ns = argparse.Namespace(query=["content:" + " ".join(args.terms)] + (args.filters or []), scope="files",
                                sort="relevance", reverse=False, limit=args.limit, json=args.json, paths=False,
                                in_=args.in_, refresh=False, no_refresh=True)
        ns.query = [f'content:"{" ".join(args.terms)}"'] + (args.filters or [])
        return cmd_search(ns, cfg)
    return 1


def cmd_topics(args, cfg):
    if args.action == "sync":
        st = topics.sync(cfg, only=parse_topic_sources(args.source or ""))
        print(json.dumps(st, ensure_ascii=False))
        return 0
    if args.action in ("show", "open"):
        t = topics.get(args.rest[0]) if args.rest else None
        if not t:
            print("找不到这个话题 key。")
            return 1
        if args.action == "show":
            print(f"{t['title']}  [{topics.PROVIDER_ZH[t['provider']]}]  {t['project']}")
            for role, body in topics.messages(args.rest[0], args.limit):
                print(("\n▶ 用户：" if role == "user" else "\n◀ 助手：") + body[:1200])
            return 0
        if args.terminal and t.get("resume") and not t["resume"].startswith("open "):
            actions.resume_in_terminal(t["resume"])
            print("已在终端中继续会话。")
        elif t.get("open_url"):
            actions.open_path(t["open_url"])
            print("已打开：" + t["open_url"])
        elif t.get("resume"):
            actions.resume_in_terminal(t["resume"])
            print("已在终端中继续会话。")
        return 0
    # default: search
    ns = argparse.Namespace(query=args.rest, scope="topics", sort="relevance", reverse=False, limit=args.limit,
                            json=args.json, paths=False, in_=None, refresh=False, no_refresh=True,
                            source=args.source, archived=args.archived, no_sync=args.no_sync)
    return cmd_search(ns, cfg)


def cmd_action(args, cfg):
    path = expand(" ".join(args.path)) if args.cmd != "copy" or args.path else ""
    if args.cmd == "open":
        actions.open_path(path); print("已打开：" + abbreviate(path))
    elif args.cmd == "reveal":
        actions.reveal(path); print("已在访达中显示：" + abbreviate(path))
    elif args.cmd == "copy":
        text = os.path.basename(path) if args.name else path
        actions.copy(text); print("已拷贝：" + text)
    elif args.cmd == "preview":
        actions.preview(path); print("已用 Quick Look 预览：" + abbreviate(path))
    elif args.cmd == "trash":
        dest = actions.trash(path); print("已移到废纸篓：" + abbreviate(path) + f"（{dest}）")
    return 0


def cmd_status(args, cfg):
    print(f"simple-search {VERSION} · 数据目录 {abbreviate(os.path.dirname(indexer.index_root_dir()))}")
    print("\n[文件名索引]")
    cmd_index(argparse.Namespace(list=True), cfg)
    print("\n[内容索引]")
    cmd_content(argparse.Namespace(action="status"), cfg)
    print("\n[AI 话题]")
    st = topics.status(cfg)
    for p, v in st["sources"].items():
        print(f"- {topics.PROVIDER_ZH[p]}：{'已安装' if v['installed'] else '未检测到'} · 开关 {v['setting']}"
              + (f" · {v['topics']} 个话题" if "topics" in v else ""))
    if args.cmd == "doctor":
        print("\n[能力检测]")
        tools = content_mod.detect_tools(cfg, build_vision=False)
        checks = [
            ("拼音数据", pinyin.available()),
            ("PDF 文字（pdftotext / PDFKit）", bool(tools.get("pdftotext") or (IS_MAC and which("swiftc")))),
            ("图片 OCR（Vision / tesseract）", bool(tools.get("vision") or tools.get("tesseract") or (IS_MAC and which("swiftc")))),
            ("tesseract 语言", tools.get("tess_langs", "—")),
            ("扫描版 PDF OCR", bool((tools.get("tesseract") and tools.get("pdftoppm")) or (IS_MAC and which("swiftc")))),
            ("Spotlight 补充", bool(IS_MAC and which("mdfind"))),
            ("macOS textutil（doc/rtf）", IS_MAC),
        ]
        for name, ok in checks:
            print(f"- {name}：{ok if isinstance(ok, str) else ('✓' if ok else '✗')}")
        if IS_MAC:
            fda = os.access(os.path.join(HOME, "Library/Mail"), os.R_OK)
            print(f"- 完全磁盘访问权限：{'已授予' if fda else '未授予（可选，授予后能搜到邮件附件等）'}")
    return 0


def cmd_bookmarks(args, cfg):
    from . import webitems

    t0 = time.time()
    results, total = webitems.search_bookmarks(" ".join(args.query), cfg, args.limit)
    ms = (time.time() - t0) * 1000
    if args.json:
        print(json.dumps({"total": total, "elapsed_ms": round(ms, 2), "results": results, "note": ""},
                         ensure_ascii=False, indent=1))
        return 0
    print(f"书签 · 共 {total:,} 个，显示 {len(results)} 个 · 用时 {ms:.1f} ms")
    for i, result in enumerate(results, 1):
        print(f"{i:>3}. {result['title']}  [{result['browser']} · {date_text(result['added'])}]"
              f"\n     {result['folder']}\n     {result['url']}")
    return 0


def cmd_config(args, cfg):
    if args.set:
        for kv in args.set:
            k, _, v = kv.partition("=")
            try:
                val = json.loads(v)
            except ValueError:
                val = v
            node = cfg
            parts = k.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = val
        save_config(cfg)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_bench(args, cfg):
    text = " ".join(args.query)
    q = parse(text)
    sel = select_indexes(cfg, args.in_, "never", [], quiet=True)
    for idx, _, scope in sel:
        _ = idx.hay  # warm
        times = []
        n = 0
        for _ in range(args.runs):
            t0 = time.perf_counter()
            _, n = search.run(idx, q)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        print(f"{abbreviate(idx.root)}  {idx.count:,} 项  命中 {n:,}  中位数 {times[len(times) // 2]:.2f} ms  最快 {times[0]:.2f} ms")
    return 0


# ------------------------------------------------------------------ parser

def _search_args(p):
    p.add_argument("query", nargs="*", help="搜索内容，语法见 syntax")
    p.add_argument("--in", dest="in_", action="append", metavar="PATH", help="只在此目录下搜索（可多次）")
    p.add_argument("--scope", choices=["files", "topics", "all"], default="files", help="搜文件 / AI 话题 / 全部")
    p.add_argument("--sort", choices=["relevance", "name", "modified", "size", "path"], default="relevance")
    p.add_argument("--reverse", action="store_true", help="反转排序方向")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--paths", action="store_true", help="每行一个路径")
    p.add_argument("--refresh", action="store_true", help="搜索前先更新索引")
    p.add_argument("--no-refresh", action="store_true", help="不自动更新索引")
    p.add_argument("--source", help="AI 话题来源：claude,codex,workbuddy")
    p.add_argument("--archived", action="store_true", help="AI 话题包含已归档")
    p.add_argument("--no-sync", action="store_true", help="AI 话题不先同步")


def build_parser():
    ap = argparse.ArgumentParser(prog="simple-search", description="simple-search：本机文件名、文件内容与 AI 对话的极速搜索")
    ap.add_argument("--version", action="version", version=f"simple-search {VERSION}")
    sub = ap.add_subparsers(dest="cmd")
    _search_args(sub.add_parser("search", help="搜索（默认命令）"))
    p = sub.add_parser("index", help="建立 / 更新文件名索引")
    p.add_argument("--root", action="append", help="只处理这些目录")
    p.add_argument("--add", action="append", help="把目录加入常驻索引范围")
    p.add_argument("--remove", action="append", help="移除某个目录的索引")
    p.add_argument("--volumes", action="store_true", help="同时索引已连接的外置磁盘")
    p.add_argument("--full", action="store_true", help="完整重建（默认只更新变化的文件夹）")
    p.add_argument("--list", action="store_true", help="列出所有索引")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--volume-meta", help=argparse.SUPPRESS)
    p = sub.add_parser("update", help="快速更新全部索引")
    p.set_defaults(root=None, add=None, remove=None, volumes=False, full=False, list=False, background=False,
                   volume_meta=None)
    p = sub.add_parser("content", help="文件内容索引与搜索")
    p.add_argument("action", choices=["index", "search", "status"])
    p.add_argument("terms", nargs="*")
    p.add_argument("--root", action="append", help="内容索引范围（默认 桌面/文稿/下载/iCloud 云盘）")
    p.add_argument("--no-images", action="store_true", help="跳过图片 OCR")
    p.add_argument("--max-seconds", type=float, help="本次最多运行多少秒（可分批建立）")
    p.add_argument("--force", action="store_true", help="全部重新提取")
    p.add_argument("--in", dest="in_", action="append")
    p.add_argument("--filter", dest="filters", action="append", help="附加文件名语法，如 ext:pdf")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("topics", help="搜索本机 AI 对话（Claude Code / Codex / WorkBuddy）")
    p.add_argument("action", nargs="?", default="search")
    p.add_argument("rest", nargs="*")
    p.add_argument("--source", help="AI 对话来源：claude,codex,workbuddy")
    p.add_argument("--archived", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--terminal", action="store_true", help="open 时在终端里继续 CLI 会话")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    for name, h in (("open", "打开"), ("reveal", "在访达中显示"), ("preview", "Quick Look 预览"), ("copy", "拷贝路径"),
                    ("trash", "移到废纸篓")):
        p = sub.add_parser(name, help=h)
        p.add_argument("path", nargs="+")
        if name == "copy":
            p.add_argument("--name", action="store_true", help="只拷贝名称")
    p = sub.add_parser("bookmarks", help="搜索浏览器书签（Chrome / Edge / Brave，含书签文件夹）")
    p.add_argument("query", nargs="*")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    sub.add_parser("ui", help="简易弹窗搜索（macOS，原生窗口不可用时的备用）")
    sub.add_parser("serve", help=argparse.SUPPRESS)
    p = sub.add_parser("app", help="安装 / 卸载原生搜索窗口（macOS 菜单栏应用 + 全局热键）")
    p.add_argument("action", choices=["install", "uninstall"])
    p.add_argument("--no-open", action="store_true")
    sub.add_parser("status", help="索引与数据源状态")
    sub.add_parser("doctor", help="状态 + 能力检测")
    sub.add_parser("syntax", help="搜索语法速查")
    p = sub.add_parser("config", help="查看 / 修改设置")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="如 include_hidden=true、content.ocr_images=false")
    p = sub.add_parser("bench", help="测速")
    p.add_argument("query", nargs="*")
    p.add_argument("--in", dest="in_", action="append")
    p.add_argument("--runs", type=int, default=20)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        argv = ["search"] + argv
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.cmd in (None, "help"):
        ap.print_help()
        return 0
    try:
        if args.cmd == "search":
            return cmd_search(args, cfg)
        if args.cmd in ("index", "update"):
            return cmd_index(args, cfg)
        if args.cmd == "content":
            if args.action == "search" and not args.terms:
                ap.error("content search 需要关键词")
            return cmd_content(args, cfg)
        if args.cmd == "topics":
            if args.action not in ("search", "sync", "show", "open"):
                args.rest = [args.action] + args.rest
                args.action = "search"
            return cmd_topics(args, cfg)
        if args.cmd in ("open", "reveal", "preview", "copy", "trash"):
            return cmd_action(args, cfg)
        if args.cmd in ("status", "doctor"):
            return cmd_status(args, cfg)
        if args.cmd == "bookmarks":
            return cmd_bookmarks(args, cfg)
        if args.cmd == "serve":
            from . import server
            return server.main()
        if args.cmd == "app":
            from . import appinstall
            if args.action == "uninstall":
                appinstall.uninstall()
                print("已卸载 simple-search 窗口。")
                return 0
            r = appinstall.install(open_after=not args.no_open)
            print(f"已安装：{r['app']}（编译 {r['seconds']} 秒）。按设置的热键（默认 ⌥空格）或点菜单栏图标打开。")
            return 0
        if args.cmd == "ui":
            from . import launcher
            return launcher.main()
        if args.cmd == "syntax":
            print(SYNTAX)
            return 0
        if args.cmd == "config":
            return cmd_config(args, cfg)
        if args.cmd == "bench":
            return cmd_bench(args, cfg)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        eprint("错误：" + str(e))
        return 1
    return 0
