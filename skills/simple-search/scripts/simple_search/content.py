"""Content search: text inside documents and images, extracted and indexed on this machine.

Extractors (best available is used, nothing is uploaded):
  text / code / csv / md / html / rtf  -> read directly
  pdf     -> pdftotext | macOS PDFKit helper | pypdf  (+ OCR of scanned pages)
  docx pptx xlsx odt ods odp epub      -> parsed from the zip/XML directly (no dependencies)
  pages numbers key                    -> embedded preview PDF, or macOS textutil
  doc rtf rtfd odt webarchive          -> macOS textutil, then a plain-text fallback
  images  -> macOS Vision (text + scene labels) | tesseract (text)
Index: SQLite FTS5 trigram table (substring search, CJK friendly; 1-2 char terms fall back to a scan).
"""
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import (HOME, IS_MAC, NOISE_NAMES, SKILL_DIR, SYSTEM_NAMES, data_dir, default_content_roots, eprint,
                     ext_of, which)

TEXT_EXT = set("""txt md markdown mdx rst org csv tsv log json jsonl yaml yml toml ini cfg conf xml tex bib srt vtt
swift c h m mm cpp hpp cc js jsx mjs cjs ts tsx py rb go rs java kt php css scss less vue svelte astro sh zsh bash
sql lua dart cs r pl gradle ipynb""".split())
HTML_EXT = {"html", "htm", "xhtml"}
OOXML_EXT = {"docx", "docm", "dotx", "pptx", "pptm", "xlsx", "xlsm"}
ODF_EXT = {"odt", "ods", "odp"}
IWORK_EXT = {"pages", "numbers", "key"}
LEGACY_EXT = {"doc", "rtf", "rtfd", "wps", "webarchive", "ppt", "xls"}
IMAGE_EXT = {"png", "jpg", "jpeg", "heic", "heif", "webp", "bmp", "tif", "tiff", "gif"}
DOC_EXT = TEXT_EXT | HTML_EXT | OOXML_EXT | ODF_EXT | IWORK_EXT | LEGACY_EXT | {"pdf", "epub"}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL, size INTEGER,
  kind TEXT, extractor TEXT, status TEXT, chars INTEGER, indexed_at REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS body USING fts5(text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS info(k TEXT PRIMARY KEY, v TEXT);
"""

# A few common scene labels in Chinese so that "猫" finds photos labelled "cat".
LABEL_ZH = {
    "cat": "猫", "dog": "狗", "animal": "动物", "bird": "鸟", "people": "人物", "person": "人物", "adult": "人物",
    "child": "儿童", "baby": "婴儿", "food": "食物", "drink": "饮品", "coffee": "咖啡", "cake": "蛋糕",
    "fruit": "水果", "vegetable": "蔬菜", "flower": "花", "plant": "植物", "tree": "树", "grass": "草地",
    "sky": "天空", "cloud": "云", "sunset_sunrise": "日落 日出", "beach": "海滩", "sea": "海", "ocean": "海洋",
    "water": "水", "mountain": "山", "snow": "雪", "outdoor": "户外", "indoor": "室内", "building": "建筑",
    "structure": "建筑", "city": "城市", "street": "街道", "road": "道路", "car": "汽车", "vehicle": "车辆",
    "bicycle": "自行车", "train": "火车", "aircraft": "飞机", "boat": "船", "document": "文档", "paper": "纸张",
    "screenshot": "截图", "text": "文字", "receipt": "小票 收据 发票", "chart": "图表", "diagram": "示意图",
    "map": "地图", "computer": "电脑", "screen": "屏幕", "phone": "手机", "keyboard": "键盘", "book": "书",
    "furniture": "家具", "table": "桌子", "chair": "椅子", "room": "房间", "kitchen": "厨房", "night": "夜景",
    "portrait": "人像", "selfie": "自拍", "group": "合影", "party": "聚会", "wedding": "婚礼", "sport": "运动",
    "ball": "球", "toy": "玩具", "art": "艺术", "painting": "绘画", "drawing": "绘画", "logo": "标志",
    "clothing": "服装", "shoes": "鞋", "jewelry": "首饰", "money": "钱", "currency": "货币", "whiteboard": "白板",
    "handwriting": "手写", "qr_code": "二维码", "barcode": "条形码", "illustrations": "插画",
}


def db_path():
    return os.path.join(data_dir(), "content.sqlite")


def connect():
    con = sqlite3.connect(db_path(), timeout=30)
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------ helpers

def _decode(data):
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</(?:w:p|a:p|text:p|text:h|p|div|li|tr|h\d|br)\s*>|<(?:br|w:br|w:tab|text:tab|text:line-break)\s*/?>", re.I)


def _xml_text(xml):
    xml = _BLOCK.sub("\n", xml)
    xml = re.sub(r"<(script|style)\b.*?</\1>", " ", xml, flags=re.S | re.I)
    text = html.unescape(_TAG.sub("", xml))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def _num(name):
    m = re.search(r"(\d+)", os.path.basename(name))
    return int(m.group(1)) if m else 0


def _run(cmd, timeout=120, data=None):
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ------------------------------------------------------------------ extractors

def ex_text(path, limit):
    with open(path, "rb") as f:
        data = f.read(limit)
    if b"\x00" in data[:4096]:
        return ""
    text = _decode(data)
    if ext_of(path) in HTML_EXT:
        return _xml_text(text)
    if ext_of(path) == "ipynb":
        try:
            nb = json.loads(text)
            return "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))
        except ValueError:
            pass
    return text


def ex_rtf(path, limit):
    with open(path, "rb") as f:
        data = f.read(limit).decode("latin-1")
    data = re.sub(r"\\'([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), data)
    data = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), data)
    data = re.sub(r"\\par[d]?|\\line", "\n", data)
    data = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", data)
    try:
        return data.encode("latin-1", "ignore").decode("gb18030", "ignore") if re.search(r"[\x80-\xff]", data) else data
    except Exception:
        return data


def ex_ooxml(path):
    ext = ext_of(path)
    out = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if ext.startswith("doc"):
            parts = ["word/document.xml"] + sorted(n for n in names if re.match(r"word/(header|footer|footnotes|endnotes|comments)\d*\.xml$", n))
        elif ext.startswith("ppt"):
            parts = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=_num)
            parts += sorted((n for n in names if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n)), key=_num)
        else:
            parts = ["xl/workbook.xml", "xl/sharedStrings.xml"]
            parts += sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), key=_num)
        for p in parts:
            if p not in names:
                continue
            xml = z.read(p).decode("utf-8", "replace")
            if p.startswith("xl/worksheets"):
                # inline strings and numbers; shared strings are read separately
                vals = re.findall(r"<is>(.*?)</is>", xml, re.S) + re.findall(r'<c [^>]*?t="(?:n|str)?"[^>]*><v>([^<]*)</v>', xml)
                out.append(" ".join(_xml_text(v) for v in vals))
            elif p == "xl/workbook.xml":
                out.append(" ".join(re.findall(r'<sheet [^>]*name="([^"]+)"', xml)))
            else:
                if ext.startswith("ppt"):
                    xml = xml.replace("</a:p>", "</a:p>\n")
                out.append(_xml_text(xml))
    return "\n".join(t for t in out if t)


def ex_odf(path):
    with zipfile.ZipFile(path) as z:
        return _xml_text(z.read("content.xml").decode("utf-8", "replace"))


def ex_epub(path):
    out = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(z.namelist()):
            if n.lower().endswith((".xhtml", ".html", ".htm")):
                out.append(_xml_text(z.read(n).decode("utf-8", "replace")))
    return "\n".join(out)


def ex_pdf(path, tools, ocr):
    text = ""
    if tools.get("pdftotext"):
        out = _run([tools["pdftotext"], "-q", "-enc", "UTF-8", path, "-"], timeout=180)
        text = out.decode("utf-8", "replace") if out else ""
    if not text.strip() and tools.get("vision"):
        res = tools["vision"].run(path, "pdf", ocr=ocr)
        if res:
            return res.get("text", ""), "vision"
    if not text.strip():
        try:
            import pypdf  # optional
            r = pypdf.PdfReader(path)
            text = "\n".join((p.extract_text() or "") for p in r.pages[:500])
        except Exception:
            pass
    if ocr and len(text.strip()) < 40 and tools.get("tesseract") and tools.get("pdftoppm"):
        text = (text + "\n" + _ocr_pdf_tesseract(path, tools)).strip()
        return text, "pdftoppm+tesseract"
    return text, "pdftotext" if tools.get("pdftotext") else "pypdf"


def _ocr_pdf_tesseract(path, tools, max_pages=30):
    import tempfile
    tmp = tempfile.mkdtemp(prefix="simple-search-ocr-")
    try:
        _run([tools["pdftoppm"], "-r", "150", "-l", str(max_pages), "-png", path, os.path.join(tmp, "p")], timeout=300)
        out = []
        for f in sorted(os.listdir(tmp), key=_num):
            out.append(_tesseract(os.path.join(tmp, f), tools))
        return "\n".join(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tesseract(path, tools):
    out = _run([tools["tesseract"], path, "-", "-l", tools.get("tess_langs", "eng"), "--psm", "3"], timeout=120)
    text = out.decode("utf-8", "replace") if out else ""
    if tools.get("tess_langs", "").startswith("chi"):
        text = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)   # tesseract spaces out CJK
    return text


def ex_iwork(path, tools):
    try:
        with zipfile.ZipFile(path) as z:
            for n in ("QuickLook/Preview.pdf", "preview.pdf"):
                if n in z.namelist():
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as t:
                        t.write(z.read(n))
                    try:
                        return ex_pdf(t.name, tools, False)[0]
                    finally:
                        os.unlink(t.name)
    except (zipfile.BadZipFile, IsADirectoryError):
        pv = os.path.join(path, "QuickLook", "Preview.pdf")
        if os.path.exists(pv):
            return ex_pdf(pv, tools, False)[0]
    return ""


def ex_textutil(path):
    if not IS_MAC:
        return ""
    out = _run(["/usr/bin/textutil", "-convert", "txt", "-stdout", "--", path], timeout=120)
    return out.decode("utf-8", "replace") if out else ""


def ex_legacy_strings(path, limit):
    """Last resort for old binary Office files: UTF-16LE and ASCII runs."""
    with open(path, "rb") as f:
        data = f.read(limit)
    runs = re.findall(rb"(?:[\x20-\x7e]\x00|[\x00-\xff][\x4e-\x9f]|[\x0c\xff]\x30){4,}", data)
    parts = [m.decode("utf-16-le", "ignore") for m in runs]
    return "\n".join(p for p in parts if sum(c.isalnum() for c in p) > len(p) / 2)


def ex_image(path, tools, labels):
    if tools.get("vision"):
        res = tools["vision"].run(path, "image", labels=labels)
        if res is not None:
            text = res.get("text", "")
            labs = res.get("labels", [])
            if labs:
                zh = [LABEL_ZH[l] for l in labs if l in LABEL_ZH]
                text += "\n[画面] " + " ".join(l.replace("_", " ") for l in labs) + (" " + " ".join(zh) if zh else "")
            return text, "vision"
    if tools.get("tesseract"):
        return _tesseract(path, tools), "tesseract"
    return None, "none"


# ------------------------------------------------------------------ macOS Vision helper

class Vision:
    """Long-running Swift helper (Vision OCR + scene labels + PDFKit). Compiled once, then reused."""

    def __init__(self, binary):
        self.binary = binary
        self.lock = threading.Lock()
        self.proc = None

    @staticmethod
    def locate(build=True):
        if not IS_MAC:
            return None
        src = os.path.join(SKILL_DIR, "scripts", "helper", "simple_search_vision.swift")
        out_dir = os.path.join(data_dir(), "bin")
        binary = os.path.join(out_dir, "simple-search-vision")
        try:
            if os.path.exists(binary) and os.path.getmtime(binary) >= os.path.getmtime(src):
                return Vision([binary])
        except OSError:
            pass
        swiftc = which("swiftc") or ("/usr/bin/swiftc" if os.path.exists("/usr/bin/swiftc") else None)
        if build and swiftc:
            os.makedirs(out_dir, exist_ok=True)
            eprint("首次使用：正在编译 macOS 图像识别组件（约 30 秒，仅一次）…")
            r = subprocess.run([swiftc, "-O", "-o", binary, src], capture_output=True, timeout=600)
            if r.returncode == 0:
                return Vision([binary])
            eprint("编译失败，改用解释执行：" + r.stderr.decode("utf-8", "replace")[-400:])
        swift = which("swift") or ("/usr/bin/swift" if os.path.exists("/usr/bin/swift") else None)
        if swift:
            return Vision([swift, src])
        return None

    def _start(self):
        self.proc = subprocess.Popen(self.binary + ["serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def run(self, path, mode, labels=False, ocr=True):
        req = json.dumps({"path": path, "mode": mode, "labels": labels, "ocr": ocr}, ensure_ascii=False)
        with self.lock:
            for _ in range(2):
                try:
                    if self.proc is None or self.proc.poll() is not None:
                        self._start()
                    self.proc.stdin.write(req + "\n")
                    self.proc.stdin.flush()
                    line = self.proc.stdout.readline()
                    if line:
                        res = json.loads(line)
                        return None if res.get("error") else res
                except (OSError, ValueError, BrokenPipeError):
                    self.proc = None
            return None

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def detect_tools(cfg, build_vision=True):
    tools = {}
    for t in ("pdftotext", "pdftoppm", "tesseract"):
        p = which(t)
        if p:
            tools[t] = p
    if tools.get("tesseract"):
        out = _run([tools["tesseract"], "--list-langs"], timeout=10) or b""
        langs = set(out.decode("utf-8", "replace").split())
        want = [l for l in ("chi_sim", "chi_tra", "eng", "jpn") if l in langs]
        tools["tess_langs"] = "+".join(want) if want else "eng"
    if IS_MAC and (cfg.get("content") or {}).get("ocr_images", True):
        v = Vision.locate(build=build_vision)
        if v:
            tools["vision"] = v
    return tools


def extract(path, tools, cfg):
    """Return (text, extractor) or (None, reason) when the file type is not supported."""
    c = cfg.get("content") or {}
    ext = ext_of(path)
    tlimit = int(c.get("max_text_mb", 8) * 1_000_000)
    try:
        if ext in TEXT_EXT or ext in HTML_EXT:
            return ex_text(path, tlimit), "text"
        if ext == "pdf":
            return ex_pdf(path, tools, c.get("ocr_scanned_pdf", True))
        if ext in OOXML_EXT:
            return ex_ooxml(path), "ooxml"
        if ext in ODF_EXT:
            return ex_odf(path), "odf"
        if ext == "epub":
            return ex_epub(path), "epub"
        if ext in IWORK_EXT:
            t = ex_iwork(path, tools)
            return (t or ex_textutil(path)), "iwork"
        if ext in LEGACY_EXT:
            t = ex_textutil(path)
            if t:
                return t, "textutil"
            if ext == "rtf":
                return ex_rtf(path, tlimit), "rtf"
            return ex_legacy_strings(path, tlimit), "strings"
        if ext in IMAGE_EXT:
            if not c.get("ocr_images", True):
                return None, "ocr-off"
            return ex_image(path, tools, c.get("image_labels", True))
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, RuntimeError) as e:
        return "", "error:" + type(e).__name__
    return None, "unsupported"


# ------------------------------------------------------------------ indexing

def iter_files(roots, cfg, want_images=True):
    exts = DOC_EXT | (IMAGE_EXT if want_images else set())
    max_doc = int((cfg.get("content") or {}).get("max_doc_mb", 80) * 1_000_000)
    skip_names = NOISE_NAMES | SYSTEM_NAMES | set(cfg.get("exclude_names", []))
    ex_paths = [os.path.normpath(os.path.expanduser(p)) for p in
                cfg.get("exclude_paths", []) + (cfg.get("content") or {}).get("exclude_paths", [])] + [data_dir()]
    ex_ext = {e.lower().lstrip(".") for e in cfg.get("exclude_ext", [])}
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip_names
                           and not ext_of(d) in ("app", "photoslibrary", "musiclibrary", "fcpbundle")
                           and not any((dirpath + "/" + d) == p or (dirpath + "/" + d).startswith(p + "/") for p in ex_paths)
                           and not (d == "target" and os.path.exists(os.path.join(dirpath, "pom.xml")))]
            # iWork documents saved as packages are folders.
            for d in list(dirnames):
                if ext_of(d) in IWORK_EXT:
                    dirnames.remove(d)
                    yield os.path.join(dirpath, d)
            for f in filenames:
                e = ext_of(f)
                if e not in exts or e in ex_ext or f.startswith("~$") or f.startswith("._"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size > max_doc or (e in IMAGE_EXT and st.st_size < 6_000):
                    continue
                yield p


def build(roots=None, cfg=None, images=None, max_seconds=None, workers=None, force=False, quiet=False):
    cfg = cfg or {}
    c = cfg.get("content") or {}
    roots = [os.path.abspath(os.path.expanduser(r)) for r in (roots or default_content_roots(cfg))]
    images = c.get("ocr_images", True) if images is None else images
    tools = detect_tools(cfg)
    if images and not (tools.get("vision") or tools.get("tesseract")):
        if not quiet:
            eprint("提示：没有可用的 OCR（macOS 需 Xcode 命令行工具；Linux 需 tesseract），图片内容将跳过。")
        images = False
    con = connect()
    known = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM docs")}
    todo, seen = [], set()
    for p in iter_files(roots, cfg, images):
        seen.add(p)
        try:
            st = os.stat(p)
        except OSError:
            continue
        old = known.get(p)
        if not force and old and old[0] == st.st_mtime and old[1] == st.st_size:
            continue
        todo.append((p, st.st_mtime, st.st_size))
    # Prune documents that disappeared from the scanned roots.
    gone = [p for p in known if any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots) and p not in seen]
    for p in gone:
        _delete(con, p)
    con.commit()
    # Documents first (fast), images afterwards; newest first inside each group.
    todo.sort(key=lambda t: (ext_of(t[0]) in IMAGE_EXT, -t[1]))
    if not quiet:
        eprint(f"内容索引：{len(seen):,} 个可索引文件，需处理 {len(todo):,} 个，移除 {len(gone):,} 个")
    max_chars = int(c.get("max_chars", 2_000_000))
    t0, done, failed = time.time(), 0, 0
    workers = workers or max(2, min(8, os.cpu_count() or 4))
    stopped = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        it = iter(todo)
        pending = {}

        def submit():
            nonlocal stopped
            if max_seconds and time.time() - t0 > max_seconds:
                stopped = True
                return False
            try:
                p, mt, sz = next(it)
            except StopIteration:
                return False
            pending[pool.submit(extract, p, tools, cfg)] = (p, mt, sz)
            return True

        for _ in range(workers * 2):
            if not submit():
                break
        while pending:
            fut = next(as_completed(list(pending)))
            p, mt, sz = pending.pop(fut)
            try:
                text, how = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the run
                text, how = "", "error:" + type(e).__name__
            if text is not None:
                text = (text or "")[:max_chars]
                _store(con, p, mt, sz, ext_of(p), how, text)
                done += 1
                if not text.strip():
                    failed += 1
            if done and done % 100 == 0:
                con.commit()
                if not quiet:
                    eprint(f"  已处理 {done:,}/{len(todo):,}（{time.time() - t0:.0f} 秒）")
            submit()
    con.execute("INSERT OR REPLACE INTO info VALUES('updated', ?)", (str(time.time()),))
    con.execute("INSERT OR REPLACE INTO info VALUES('roots', ?)",
                (json.dumps(sorted(set(json.loads(_info(con, "roots") or "[]")) | set(roots))),))
    con.commit()
    con.close()
    if tools.get("vision"):
        tools["vision"].close()
    left = len(todo) - done
    return {"indexed": done, "empty": failed, "removed": len(gone), "remaining": left if stopped else 0,
            "seconds": round(time.time() - t0, 1), "roots": roots,
            "ocr": "vision" if tools.get("vision") else ("tesseract" if tools.get("tesseract") else None)}


def _info(con, k):
    r = con.execute("SELECT v FROM info WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _delete(con, path):
    r = con.execute("SELECT id FROM docs WHERE path=?", (path,)).fetchone()
    if r:
        con.execute("DELETE FROM body WHERE rowid=?", (r[0],))
        con.execute("DELETE FROM docs WHERE id=?", (r[0],))


def _store(con, path, mtime, size, kind, how, text):
    _delete(con, path)
    cur = con.execute("INSERT INTO docs(path, mtime, size, kind, extractor, status, chars, indexed_at) VALUES(?,?,?,?,?,?,?,?)",
                      (path, mtime, size, kind, how, "ok" if text.strip() else "empty", len(text), time.time()))
    con.execute("INSERT INTO body(rowid, text) VALUES(?, ?)", (cur.lastrowid, text))


# ------------------------------------------------------------------ searching

def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def fts_phrase(terms):
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def snippet(text, terms, width=70):
    low = text.lower()
    best = None
    for t in terms:
        p = low.find(t.lower())
        if p >= 0 and (best is None or p < best[0]):
            best = (p, len(t))
    if best is None:
        return text[:width * 2].replace("\n", " ").strip()
    p, n = best
    a, b = max(0, p - width), min(len(text), p + n + width)
    s = text[a:b].replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    for t in sorted(terms, key=len, reverse=True):
        s = re.sub(re.escape(t), lambda m: "【" + m.group(0) + "】", s, flags=re.I)
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def split_terms(value):
    """content:"a b" is one phrase; content:a,b or a b are separate AND terms."""
    return [t for t in re.split(r"[,，]", value) if t.strip()] if ("," in value or "，" in value) else [value]


def search(terms, negated=(), limit=5000, scope=None):
    """Paths whose extracted text contains every term. Returns [(path, snippet, hits)]."""
    if not os.path.exists(db_path()):
        return []
    con = connect()
    sql = "SELECT d.path, b.text FROM body b JOIN docs d ON d.id=b.rowid WHERE 1=1"
    args = []
    long_terms = [t for t in terms if len(t) >= 3]
    if long_terms:   # trigram index lookup
        sql += " AND body MATCH ?"
        args.append(fts_phrase(long_terms))
    for t in terms:
        if len(t) < 3:
            sql += " AND b.text LIKE ? ESCAPE '\\'"
            args.append(_like(t))
    for t in negated:
        sql += " AND b.text NOT LIKE ? ESCAPE '\\'"
        args.append(_like(t))
    if scope:
        sql += " AND (d.path = ? OR d.path LIKE ? ESCAPE '\\')"
        args += [scope, scope.rstrip("/").replace("%", "\\%").replace("_", "\\_") + "/%"]
    sql += " LIMIT ?"
    args.append(limit)
    out = []
    for path, text in con.execute(sql, args):
        low = text.lower()
        hits = sum(low.count(t.lower()) for t in terms)
        out.append((path, snippet(text, terms), hits))
    con.close()
    return out


def spotlight(terms, scope=None, limit=2000):
    """macOS Spotlight as an extra source (it already indexes many documents and Live Text in images)."""
    if not IS_MAC or not which("mdfind"):
        return []
    parts = []
    for t in terms:
        t = t.replace('"', '\\"')
        parts.append(f'(kMDItemTextContent == "*{t}*"cd || kMDItemDisplayName == "*{t}*"cd)')
    cmd = ["mdfind"] + (["-onlyin", scope] if scope else ["-onlyin", HOME]) + [" && ".join(parts)]
    out = _run(cmd, timeout=20)
    if not out:
        return []
    return [p for p in out.decode("utf-8", "replace").splitlines() if p][:limit]


def status():
    if not os.path.exists(db_path()):
        return {"exists": False}
    con = connect()
    total = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    by = dict(con.execute("SELECT kind, COUNT(*) FROM docs GROUP BY kind ORDER BY 2 DESC").fetchall())
    empty = con.execute("SELECT COUNT(*) FROM docs WHERE status='empty'").fetchone()[0]
    upd = _info(con, "updated")
    roots = json.loads(_info(con, "roots") or "[]")
    con.close()
    return {"exists": True, "documents": total, "empty": empty, "by_type": by, "roots": roots,
            "updated": float(upd) if upd else None, "size_mb": round(os.path.getsize(db_path()) / 1e6, 1)}
