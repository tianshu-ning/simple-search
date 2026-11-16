"""Name index: scanning, persistence and incremental refresh.

Layout (columnar, one row per entry, rows in pre-order so that the subtree of a
folder r is exactly rows r..end[r]):
  names   '\n'-joined original names  (searched with str.find / re in C)
  parent  int32   end int32   flags uint8   kind uint8   depth uint8
  size    int64   mtime float64
  pinyin  '\n'-joined keys for names containing Han characters, with a row map
"""
import array
import bisect
import hashlib
import json
import os
import shutil
import stat as _stat
import time
from concurrent.futures import ThreadPoolExecutor

from . import pinyin
from .common import (BUILD_OUTPUTS, F_DIR, F_HIDDEN, F_INPKG, F_LINK, F_NOISE, F_PACKAGE, F_USER, HOME, IS_MAC,
                     NOISE_NAMES, PACKAGE_EXTS, SYSTEM_NAMES, TOP_SYSTEM, VERSION, data_dir, eprint,
                     ext_of, kind_of)

FORMAT = 3
SCANNER_RULES = 2   # bump when built-in skip rules change
UF_HIDDEN = 0x8000


def index_root_dir():
    p = os.path.join(data_dir(), "indexes")
    os.makedirs(p, exist_ok=True)
    return p


def slug(root):
    base = os.path.basename(root.rstrip("/")) or "root"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base)[:40]
    return f"{safe}-{hashlib.sha1(root.encode()).hexdigest()[:10]}"


# ------------------------------------------------------------------ rules

class Rules:
    def __init__(self, cfg, root):
        self.root = root
        self.include_hidden = cfg.get("include_hidden", False)
        self.include_deps = cfg.get("include_deps", False)
        self.include_packages = cfg.get("include_packages", False)
        self.include_library = cfg.get("include_library", False)
        self.ex_paths = [os.path.normpath(os.path.expanduser(p)) for p in cfg.get("exclude_paths", [])]
        self.ex_paths.append(data_dir())
        self.ex_names = set(cfg.get("exclude_names", [])) | SYSTEM_NAMES
        self.ex_ext = {e.lower().lstrip(".") for e in cfg.get("exclude_ext", [])}
        self.user_lib = os.path.join(HOME, "Library")
        self.root_dev = None
        try:
            self.root_dev = os.stat(root).st_dev
        except OSError:
            pass

    @staticmethod
    def is_build_output(path, name):
        parent = os.path.dirname(path)
        return any(os.path.exists(os.path.join(parent, f)) for f in BUILD_OUTPUTS[name])

    def skip_entry(self, name, is_dir):
        if name in self.ex_names or "\n" in name:
            return True
        return not is_dir and self.ex_ext and ext_of(name) in self.ex_ext

    def descend(self, path, name, depth, flags, dev):
        """Should we list the children of this folder?"""
        if flags & F_LINK:
            return False
        if flags & F_PACKAGE and not self.include_packages:
            return False
        if name in NOISE_NAMES and not self.include_deps:
            return False
        if name in BUILD_OUTPUTS and not self.include_deps and self.is_build_output(path, name):
            return False
        if flags & F_HIDDEN and not self.include_hidden:
            return False
        if self.root == "/" and depth == 1 and name in TOP_SYSTEM:
            return False
        if path == self.user_lib and not self.include_library:
            return False
        if IS_MAC and self.root == "/" and path in ("/Volumes", "/System/Volumes"):
            return False
        if dev is not None and self.root_dev is not None and dev != self.root_dev:
            return False          # other mounts get their own index
        for p in self.ex_paths:
            if path == p or path.startswith(p + "/"):
                return False
        return True


# ------------------------------------------------------------------ storage

class Index:
    """Loaded index. Attribute names mirror the on-disk columns."""

    def __init__(self):
        self.meta = {}
        self.names = []
        self.parent = array.array("i")
        self.end = array.array("i")
        self.flags = array.array("B")
        self.kind = array.array("B")
        self.depth = array.array("B")
        self.size = array.array("q")
        self.mtime = array.array("d")
        self.py_text = ""
        self.py_rows = array.array("i")
        self.py_starts = None
        self._hay = None
        self._lower_names = None
        self._starts = None
        self._paths = {}
        self.path_dir = None

    # ----- derived, built lazily
    @property
    def root(self):
        return self.meta.get("root", "/")

    @property
    def count(self):
        return len(self.names)

    def _build_hay(self):
        text = "\n".join(self.names)
        low = text.lower()
        if len(low) == len(text):
            self._hay = low
            self._lower_names = None
        else:  # rare: lower() changed the length of some character
            self._lower_names = [n.lower() for n in self.names]
            self._hay = "\n".join(self._lower_names)
        lens = self._lower_names or self.names
        starts = array.array("q", [0]) * len(lens)
        pos = 0
        for i, n in enumerate(lens):
            starts[i] = pos
            pos += len(n) + 1
        self._starts = starts

    @property
    def hay(self):
        if self._hay is None:
            self._build_hay()
        return self._hay

    @property
    def starts(self):
        if self._starts is None:
            self._build_hay()
        return self._starts

    def lower(self, i):
        if self._hay is None:
            self._build_hay()
        if self._lower_names is not None:
            return self._lower_names[i]
        return self.names[i].lower()

    def row_at(self, offset):
        return bisect.bisect_right(self.starts, offset) - 1

    def path(self, i):
        p = self._paths.get(i)
        if p is not None:
            return p
        parts = []
        j = i
        while j > 0:
            parts.append(self.names[j])
            j = self.parent[j]
        root = self.root
        if not parts:
            p = root
        else:
            parts.reverse()
            p = (root.rstrip("/") + "/" + "/".join(parts))
        if len(self._paths) < 200_000:
            self._paths[i] = p
        return p

    def py_row_at(self, offset):
        if self.py_starts is None:
            st = array.array("q", [0]) * len(self.py_rows)
            pos = 0
            for k, line in enumerate(self.py_text.split("\n")) if self.py_text else []:
                st[k] = pos
                pos += len(line) + 1
            self.py_starts = st
        return bisect.bisect_right(self.py_starts, offset) - 1

    def find_dir(self, path):
        """Row of an indexed folder path, or None."""
        path = os.path.normpath(path)
        root = self.root.rstrip("/") or "/"
        if path == root:
            return 0
        if not path.startswith(root.rstrip("/") + "/"):
            return None
        rel = path[len(root.rstrip("/")) + 1:].split("/")
        row = 0
        for comp in rel:
            # children of row are inside row+1..end[row]; walk siblings by jumping subtrees
            j, stop, found = row + 1, self.end[row], None
            while j <= stop:
                if self.names[j] == comp:
                    found = j
                    break
                j = self.end[j] + 1
            if found is None:
                return None
            row = found
        return row

    def age(self):
        return time.time() - self.meta.get("updated", 0)

    # ----- persistence
    @staticmethod
    def location(root):
        return os.path.join(index_root_dir(), slug(root))

    def save(self, folder=None):
        folder = folder or Index.location(self.root)
        tmp = folder + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, mode=0o700)
        with open(os.path.join(tmp, "names.txt"), "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(self.names))
        cols = {}
        with open(os.path.join(tmp, "cols.bin"), "wb") as f:
            for name in ("parent", "end", "flags", "kind", "depth", "size", "mtime", "py_rows"):
                a = getattr(self, name)
                cols[name] = [a.typecode, len(a), f.tell()]
                a.tofile(f)
        with open(os.path.join(tmp, "pinyin.txt"), "w", encoding="utf-8") as f:
            f.write(self.py_text)
        self.meta.update({"format": FORMAT, "version": VERSION, "count": self.count, "columns": cols})
        with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False)
        old = folder + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(folder):
            os.replace(folder, old)
        os.replace(tmp, folder)
        shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def load(folder):
        idx = Index()
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            idx.meta = json.load(f)
        if idx.meta.get("format") != FORMAT:
            raise ValueError("index format changed")
        with open(os.path.join(folder, "names.txt"), encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        idx.names = text.split("\n") if idx.meta.get("count", 0) else []
        with open(os.path.join(folder, "cols.bin"), "rb") as f:
            for name, (tc, n, off) in idx.meta["columns"].items():
                a = array.array(tc)
                f.seek(off)
                a.fromfile(f, n)
                setattr(idx, name, a)
        try:
            with open(os.path.join(folder, "pinyin.txt"), encoding="utf-8") as f:
                idx.py_text = f.read()
        except OSError:
            idx.py_text = ""
        if len(idx.names) != len(idx.parent):
            raise ValueError("damaged index")
        return idx


def list_indexes():
    out = []
    base = index_root_dir()
    for d in sorted(os.listdir(base)):
        if d.endswith((".tmp", ".old")):
            continue
        meta = os.path.join(base, d, "meta.json")
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
            m["_folder"] = os.path.join(base, d)
            out.append(m)
        except (OSError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ scanning

def _stat_entry(entry):
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return st


def _list_dir(path, rules, depth, inherited, old):
    """List a folder. Returns [(name, flags, kind, size, mtime, dev)] sorted for stable order."""
    reuse = old.get(path) if old is not None else None
    out = []
    if reuse is not None and reuse[0] is not None:
        # Folder unchanged since last scan: reuse children, only re-stat sub-folders.
        for name, fl, kd, sz, mt in reuse[1]:
            dev = None
            if fl & F_DIR and not fl & F_LINK:
                try:
                    st = os.lstat(os.path.join(path, name))
                    mt, dev = st.st_mtime, st.st_dev
                except OSError:
                    continue
            out.append((name, fl, kd, sz, mt, dev))
        return out
    try:
        it = os.scandir(path)
    except OSError:
        return out
    with it:
        for e in it:
            name = e.name
            try:
                is_link = e.is_symlink()
                is_dir = (not is_link) and e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if rules.skip_entry(name, is_dir):
                continue
            st = _stat_entry(e)
            if st is None:
                continue
            fl = inherited
            if is_dir:
                fl |= F_DIR
            if is_link:
                fl |= F_LINK
            if name.startswith(".") or (getattr(st, "st_flags", 0) & UF_HIDDEN):
                fl |= F_HIDDEN
            pkg = is_dir and ext_of(name) in PACKAGE_EXTS
            if pkg:
                fl |= F_PACKAGE
            out.append((name, fl, kind_of(name, is_dir, pkg), 0 if is_dir else st.st_size, st.st_mtime,
                        st.st_dev))
    out.sort(key=lambda t: (not (t[1] & F_DIR), t[0].lower()))
    return out


class _Cols:
    __slots__ = ("names", "parent", "end", "flags", "kind", "depth", "size", "mtime")

    def __init__(self):
        self.names = []
        self.parent = array.array("i")
        self.end = array.array("i")
        self.flags = array.array("B")
        self.kind = array.array("B")
        self.depth = array.array("B")
        self.size = array.array("q")
        self.mtime = array.array("d")

    def add(self, name, parent, flags, kind, depth, size, mtime):
        r = len(self.names)
        self.names.append(name)
        self.parent.append(parent)
        self.end.append(r)
        self.flags.append(flags & 0xFF)
        self.kind.append(kind)
        self.depth.append(min(depth, 255))
        self.size.append(size)
        self.mtime.append(mtime)
        return r

    def extend(self, other, parent_row):
        off = len(self.names)
        self.names.extend(other.names)
        self.parent.extend(array.array("i", (parent_row if p < 0 else p + off for p in other.parent)))
        self.end.extend(array.array("i", (e + off for e in other.end)))
        for n in ("flags", "kind", "depth", "size", "mtime"):
            getattr(self, n).extend(getattr(other, n))


def _child_inherited(flags, path, rules):
    f = flags & (F_NOISE | F_INPKG | F_USER)
    name = os.path.basename(path)
    if name in NOISE_NAMES or flags & F_HIDDEN or (name in BUILD_OUTPUTS and Rules.is_build_output(path, name)):
        f |= F_NOISE
    if flags & F_PACKAGE:
        f |= F_INPKG
    if path == HOME or path.startswith(HOME + "/"):
        f |= F_USER
    return f


def _scan_subtree(path, depth, flags, rules, old, progress):
    """Scan the contents of `path` (not the folder itself). parent=-1 means 'the folder'."""
    cols = _Cols()
    stack = [(_list_dir(path, rules, depth, _child_inherited(flags, path, rules), old), 0, -1, path, depth)]
    while stack:
        entries, i, prow, ppath, d = stack[-1]
        if i >= len(entries):
            stack.pop()
            if prow >= 0:
                cols.end[prow] = len(cols.names) - 1
            continue
        stack[-1] = (entries, i + 1, prow, ppath, d)
        name, fl, kd, sz, mt, dev = entries[i]
        row = cols.add(name, prow, fl, kd, d + 1, sz, mt)
        if fl & F_DIR:
            cpath = ppath.rstrip("/") + "/" + name
            if rules.descend(cpath, name, d + 1, fl, dev):
                kids = _list_dir(cpath, rules, d + 1, _child_inherited(fl, cpath, rules), old)
                stack.append((kids, 0, row, cpath, d + 1))
                progress[0] += 1
                if progress[0] % 5000 == 0:
                    eprint(f"  已扫描 {progress[0]:,} 个文件夹…")
    return cols


def _old_listing(idx):
    """path -> (mtime, [(name, flags, kind, size, mtime) ...]) for every folder of an old index."""
    if idx is None:
        return None
    kids = {}
    for i in range(1, idx.count):
        kids.setdefault(idx.parent[i], []).append(i)
    out = {}
    for r, ch in kids.items():
        if not idx.flags[r] & F_DIR:
            continue
        out[idx.path(r)] = (idx.mtime[r], [(idx.names[c], idx.flags[c], idx.kind[c], idx.size[c], idx.mtime[c])
                                           for c in ch])
    idx._paths.clear()
    return out


def _reuse_map(old_listing, rules):
    """Wrap listings so that _list_dir only reuses folders whose mtime is unchanged."""
    if not old_listing:
        return None

    class M(dict):
        def get(self, path, default=None):
            v = old_listing.get(path)
            if v is None:
                return None
            try:
                mt = os.lstat(path).st_mtime
            except OSError:
                return None
            return v if mt == v[0] else None
    return M()


def build(root, cfg, old=None, workers=None, extra_meta=None, quiet=False):
    """Scan `root` and return a new Index. With `old`, unchanged folders are reused."""
    t0 = time.time()
    rules = Rules(cfg, root)
    reuse = _reuse_map(_old_listing(old), rules) if old is not None else None
    try:
        st = os.stat(root)
    except OSError as e:
        raise RuntimeError(f"无法读取 {root}: {e}")
    root_flags = F_DIR | (F_USER if (root == HOME or root.startswith(HOME + "/")) else 0)
    cols = _Cols()
    cols.add(os.path.basename(root.rstrip("/")) or "/", -1, root_flags, 1, 0, 0, st.st_mtime)
    top = _list_dir(root, rules, 0, _child_inherited(root_flags, root, rules), reuse)
    progress = [0]
    workers = workers or min(16, (os.cpu_count() or 4) * 2)
    jobs = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for k, (name, fl, kd, sz, mt, dev) in enumerate(top):
            if fl & F_DIR:
                cpath = root.rstrip("/") + "/" + name
                if rules.descend(cpath, name, 1, fl, dev):
                    jobs[k] = pool.submit(_scan_subtree, cpath, 1, fl, rules, reuse, progress)
        for k, (name, fl, kd, sz, mt, dev) in enumerate(top):
            row = cols.add(name, 0, fl, kd, 1, sz, mt)
            if k in jobs:
                cols.extend(jobs[k].result(), row)
                cols.end[row] = len(cols.names) - 1
    cols.end[0] = len(cols.names) - 1

    idx = Index()
    for n in _Cols.__slots__:
        setattr(idx, n, getattr(cols, n))
    _build_pinyin(idx)
    idx.meta = {"root": root, "created": (old.meta.get("created") if old else time.time()),
                "updated": time.time(), "scan_seconds": round(time.time() - t0, 2),
                "rules": _rules_key(cfg)}
    if extra_meta:
        idx.meta.update(extra_meta)
    if not quiet:
        eprint(f"  {root}: {idx.count:,} 项，用时 {time.time() - t0:.1f} 秒")
    return idx


def _build_pinyin(idx):
    if not pinyin.available():
        idx.py_text, idx.py_rows = "", array.array("i")
        return
    lines, rows = [], array.array("i")
    for i, n in enumerate(idx.names):
        if not pinyin.has_han(n):
            continue
        for full, init in pinyin.keys(n.lower()):
            lines.append(full); rows.append(i)
            if init != full:
                lines.append(init); rows.append(i)
    idx.py_text = "\n".join(lines)
    idx.py_rows = rows


def _rules_key(cfg):
    keys = ("exclude_paths", "exclude_names", "exclude_ext", "include_hidden", "include_deps",
            "include_packages", "include_library")
    # Scanner rule changes between releases also invalidate reused folder listings.
    rules = {k: cfg.get(k) for k in keys}
    rules["_scanner"] = SCANNER_RULES
    return hashlib.sha1(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ manager

def load_for(root):
    folder = Index.location(root)
    if not os.path.isdir(folder):
        return None
    try:
        return Index.load(folder)
    except (OSError, ValueError, KeyError):
        return None


def refresh(root, cfg, full=False, extra_meta=None, quiet=False):
    old = None if full else load_for(root)
    if old is not None and old.meta.get("rules") != _rules_key(cfg):
        old = None   # exclusion rules changed: rebuild everything
    # Reused folders keep their files' size/mtime; a daily full pass picks up in-place edits.
    if old is not None and time.time() - old.meta.get("full_at", 0) > 86400:
        old = None
    was_full = old is None
    if not quiet:
        eprint(("快速更新" if old else "建立索引") + f"：{root}")
    meta = dict(old.meta) if old else {}
    meta.pop("columns", None)
    meta.update(extra_meta or {})
    idx = build(root, cfg, old=old, extra_meta=meta, quiet=quiet)
    if was_full:
        idx.meta["full_at"] = time.time()
    idx.save()
    return idx


def lock_path(root):
    return Index.location(root) + ".lock"


def refreshing(root):
    p = lock_path(root)
    try:
        return time.time() - os.path.getmtime(p) < 900
    except OSError:
        return False


def background_refresh(root, extra_meta=None):
    """Start a detached quick update so the current search is not delayed."""
    import subprocess
    import sys
    if refreshing(root):
        return False
    open(lock_path(root), "w").close()
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "simple_search_cli.py")
    args = [sys.executable, "-I", script, "index", "--root", root, "--background"]
    if extra_meta and extra_meta.get("volume"):
        args.append("--volume-meta=" + json.dumps(extra_meta, ensure_ascii=False))
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    return True


def remove(root):
    shutil.rmtree(Index.location(root), ignore_errors=True)


# ------------------------------------------------------------------ volumes

def mounted_volumes():
    """External / removable volumes that deserve their own catalog."""
    vols = []
    cands = []
    if IS_MAC:
        cands = [os.path.join("/Volumes", v) for v in _safe_listdir("/Volumes")]
    else:
        user = os.environ.get("USER", "")
        for base in (f"/media/{user}", f"/run/media/{user}", "/media", "/mnt"):
            cands += [os.path.join(base, v) for v in _safe_listdir(base)]
    seen = set()
    try:
        root_dev = os.stat("/").st_dev
    except OSError:
        root_dev = None
    for p in cands:
        try:
            if os.path.islink(p) or not os.path.isdir(p) or not os.path.ismount(p):
                continue
            st = os.stat(p)
        except OSError:
            continue
        if st.st_dev == root_dev or st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        vols.append({"path": p, "label": os.path.basename(p), "uuid": volume_uuid(p)})
    return vols


def _safe_listdir(p):
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


def volume_uuid(path):
    if IS_MAC:
        import plistlib
        import subprocess
        try:
            out = subprocess.run(["diskutil", "info", "-plist", path], capture_output=True, timeout=5).stdout
            info = plistlib.loads(out)
            return info.get("VolumeUUID") or info.get("DiskUUID") or ""
        except Exception:
            return ""
    try:
        vs = os.statvfs(path)
        return f"{vs.f_blocks}-{vs.f_frsize}"
    except OSError:
        return ""
