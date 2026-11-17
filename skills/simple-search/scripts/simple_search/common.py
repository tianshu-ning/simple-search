"""simple-search 的共享配置、常量和基础工具。"""
import json
import os
import sys
import time

VERSION = "1.2.1"
HOME = os.path.expanduser("~")
IS_MAC = sys.platform == "darwin"
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Entry flags (bit field, one byte per entry).
F_DIR, F_LINK, F_HIDDEN, F_NOISE, F_INPKG, F_PACKAGE, F_USER = 1, 2, 4, 16, 32, 64, 128

KIND_NAMES = ["folder", "app", "doc", "image", "video", "audio", "code", "archive"]
KIND_ZH = {0: "文件", 1: "文件夹", 2: "应用", 3: "文档", 4: "图片", 5: "视频", 6: "音频", 7: "代码", 8: "压缩包"}
KIND_ALIASES = {
    "dir": "folder", "directory": "folder", "文件夹": "folder", "目录": "folder",
    "application": "app", "应用": "app", "document": "doc", "docs": "doc", "文档": "doc",
    "img": "image", "pic": "image", "photo": "image", "picture": "image", "图片": "image", "照片": "image",
    "movie": "video", "视频": "video", "music": "audio", "sound": "audio", "音频": "audio", "音乐": "audio",
    "source": "code", "代码": "code", "zip": "archive", "压缩包": "archive",
}


def _words(s):
    return set(s.split())


EXT_GROUPS = {
    3: _words("pdf doc docx xls xlsx ppt pptx pages numbers key txt md markdown rtf rtfd csv tsv epub mobi odt ods odp tex log wps et dps"),
    4: _words("png jpg jpeg gif heic heif webp bmp tiff tif svg psd ai sketch fig raw cr2 cr3 nef arw dng ico icns avif"),
    5: _words("mp4 mov mkv avi wmv flv webm m4v mpg mpeg 3gp rmvb"),
    6: _words("mp3 wav flac aac m4a ogg wma aiff aif opus mid midi caf"),
    7: _words("swift c h m mm cpp hpp cc js jsx mjs cjs ts tsx py rb go rs java kt php html htm css scss less vue svelte astro sh zsh bash json yaml yml toml xml sql lua dart cs r pl gradle plist ipynb"),
    8: _words("zip rar 7z tar gz bz2 xz tgz zst dmg iso pkg jar war apk ipa"),
}
EXT_KIND = {e: k for k, exts in EXT_GROUPS.items() for e in exts}
PACKAGE_EXTS = _words(
    "app bundle framework plugin kext appex xpc prefpane qlgenerator mdimporter saver component vst vst3 "
    "photoslibrary musiclibrary tvlibrary fcpbundle imovielibrary xcodeproj xcworkspace playground xcarchive "
    "dsym rtfd scptd lproj logicx band screenstudio pages numbers key")
# Dependency / cache folders: listed, but their contents are skipped by default.
NOISE_NAMES = _words(
    "node_modules .git .svn .hg __pycache__ .venv venv site-packages Pods DerivedData .gradle .npm .pnpm-store "
    ".yarn .cache .cargo .rustup .Trash Caches Cache .next .nuxt bower_components .idea .build .swiftpm "
    "CMakeFiles .tox .mypy_cache .pytest_cache .terraform")
SYSTEM_NAMES = {".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems", ".Trashes",
                ".MobileBackups", "$RECYCLE.BIN", "System Volume Information"}
# Build output folders, treated like dependency folders when a build file sits next to them.
BUILD_OUTPUTS = {"target": ("pom.xml", "Cargo.toml", "build.gradle", "build.gradle.kts", "build.sbt", "project.clj")}
TOP_SYSTEM = _words("System private usr bin sbin opt Library cores dev proc sys run snap boot lost+found")

DEFAULT_CONFIG = {
    "roots": [],                 # empty -> home folder (+ /Applications on macOS)
    "exclude_paths": [],
    "exclude_names": [],
    "exclude_ext": [],
    "include_hidden": False,
    "include_deps": False,
    "include_packages": False,
    "include_library": False,
    "auto_refresh_minutes": 30,
    "index_volumes": True,
    "verify_results": True,
    "topics": {"claude": None, "codex": None, "workbuddy": None},
    "bookmarks": {"files": []},  # extra Chromium "Bookmarks" files; installed browsers are found automatically
    "web_shortcuts": [],
    "content": {
        "roots": [],             # empty -> Desktop, Documents, Downloads (+ iCloud Drive on macOS)
        "exclude_paths": [],     # folders skipped by content indexing only (e.g. code repositories)
        "max_doc_mb": 80,
        "max_text_mb": 8,
        "max_chars": 2_000_000,
        "ocr_images": True,
        "image_labels": True,
        "ocr_scanned_pdf": True,
        "spotlight": "auto",
    },
}


def data_dir():
    path = os.environ.get("SIMPLE_SEARCH_HOME") or os.path.join(HOME, ".simple-search")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def config_path():
    return os.path.join(data_dir(), "config.json")


def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = None
    cfg = _merge(DEFAULT_CONFIG, saved if isinstance(saved, dict) else {})
    if isinstance(cfg.get("topics"), dict):
        cfg["topics"].pop("deepseek", None)
    return cfg


def save_config(cfg):
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path())


def default_roots(cfg):
    roots = [expand(r) for r in cfg.get("roots") or []]
    if not roots:
        roots = [HOME]
        if IS_MAC and os.path.isdir("/Applications"):
            roots.append("/Applications")
    return [r for r in roots if os.path.isdir(r)]


def default_content_roots(cfg):
    roots = [expand(r) for r in (cfg.get("content") or {}).get("roots") or []]
    if not roots:
        roots = [os.path.join(HOME, d) for d in ("Desktop", "Documents", "Downloads")]
        icloud = os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
        if IS_MAC and os.path.isdir(icloud):
            roots.append(icloud)
    return [r for r in roots if os.path.isdir(r)]


def expand(p):
    p = os.path.expanduser(p.strip())
    return os.path.normpath(os.path.abspath(p)) if p else p


def abbreviate(path):
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~" + path[len(HOME):]
    return path


def ext_of(name):
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1 or len(name) - dot - 1 > 8:
        return ""
    return name[dot + 1:].lower()


def kind_of(name, is_dir, is_package):
    ext = ext_of(name)
    if is_dir and is_package and ext == "app":
        return 2
    if is_dir and not is_package:
        return 1
    return EXT_KIND.get(ext, 0)


def size_text(n):
    if n is None:
        return ""
    if n < 1000:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1000.0
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if n >= 100 else f"{n:.1f} {unit}"
    return ""


def date_text(ts, now=None):
    if not ts:
        return ""
    now = now or time.time()
    t, n = time.localtime(ts), time.localtime(now)
    if t[:3] == n[:3]:
        return time.strftime("今天 %H:%M", t)
    if time.localtime(now - 86400)[:3] == t[:3]:
        return time.strftime("昨天 %H:%M", t)
    if t.tm_year == n.tm_year:
        return f"{t.tm_mon}月{t.tm_mday}日"
    return f"{t.tm_year}/{t.tm_mon}/{t.tm_mday}"


def age_text(seconds):
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def which(cmd):
    for d in os.environ.get("PATH", "").split(os.pathsep) + ["/opt/homebrew/bin", "/usr/local/bin"]:
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None
