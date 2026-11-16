"""Actions on results. On macOS these use the system tools; elsewhere they degrade gracefully."""
import os
import shlex
import subprocess
import tempfile
import time

from .common import HOME, IS_MAC, which


def _run(cmd, data=None):
    r = subprocess.run(cmd, input=data, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip() or f"{cmd[0]} 失败")


def open_path(path):
    if IS_MAC:
        _run(["open", "--", path])
    elif which("xdg-open"):
        _run(["xdg-open", path])
    else:
        raise RuntimeError("此环境无法打开图形界面应用；请在本机执行：open " + shlex.quote(path))


def reveal(path):
    if IS_MAC:
        _run(["open", "-R", "--", path])
    elif which("xdg-open"):
        _run(["xdg-open", os.path.dirname(path)])
    else:
        raise RuntimeError("此环境无法打开文件管理器")


def preview(path):
    if IS_MAC:
        subprocess.Popen(["qlmanage", "-p", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        open_path(path)


def copy(text):
    if IS_MAC:
        _run(["pbcopy"], text.encode("utf-8"))
    elif which("wl-copy"):
        _run(["wl-copy"], text.encode("utf-8"))
    elif which("xclip"):
        _run(["xclip", "-selection", "clipboard"], text.encode("utf-8"))
    else:
        raise RuntimeError("没有可用的剪贴板工具")


def trash(path):
    """Move to the Trash (recoverable). Never deletes permanently."""
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        raise RuntimeError("不存在：" + path)
    if IS_MAC:
        script = 'on run argv\ntell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\nend run'
        try:
            _run(["osascript", "-e", script, path])
            return "废纸篓（可在访达中「放回原处」）"
        except RuntimeError:
            dest = os.path.join(HOME, ".Trash")
    else:
        dest = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")), "Trash", "files")
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, os.path.basename(path))
    if os.path.lexists(target):
        base, ext = os.path.splitext(os.path.basename(path))
        target = os.path.join(dest, f"{base} {time.strftime('%H.%M.%S')}{ext}")
    os.rename(path, target)
    if not IS_MAC:
        info = os.path.join(os.path.dirname(dest), "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, os.path.basename(target) + ".trashinfo"), "w") as f:
            f.write(f"[Trash Info]\nPath={path}\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    return target


def resume_in_terminal(command):
    """Open Terminal and run a resume command (no prompt is sent to the agent)."""
    if not IS_MAC:
        raise RuntimeError("请在终端中执行：" + command)
    d = os.path.join(tempfile.gettempdir(), "simple-search-resume")
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, p = tempfile.mkstemp(suffix=".command", dir=d)
    with os.fdopen(fd, "w") as f:
        f.write("#!/bin/zsh -l\n/bin/rm -- \"$0\"\n" + command + "\n")
    os.chmod(p, 0o700)
    _run(["open", p])
