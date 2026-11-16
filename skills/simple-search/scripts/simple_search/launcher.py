"""Quick search window for macOS (native dialogs via osascript, no extra software).

Flow: search box -> result list -> action (open / reveal / copy ...). Enter, Enter opens the top hit.
"""
import json
import os
import subprocess
import sys

from . import actions
from .common import abbreviate, data_dir, date_text

TITLE = "simple-search"

ASK = '''on run argv
activate
set r to display dialog (item 2 of argv) default answer (item 1 of argv) with title "%s" buttons {"取消", "搜索"} default button "搜索" cancel button "取消"
return text returned of r
end run''' % TITLE

CHOOSE = '''on run argv
activate
set picked to choose from list (items 3 thru -1 of argv) with title "%s" with prompt (item 1 of argv) OK button name (item 2 of argv) cancel button name "重新搜索" default items {item 3 of argv}
if picked is false then return "<<again>>"
return item 1 of picked
end run''' % TITLE

ACT = '''on run argv
activate
set b to button returned of (display dialog (item 1 of argv) with title "%s" buttons {item 2 of argv, item 3 of argv, item 4 of argv} default button (item 4 of argv))
return b
end run''' % TITLE

ALERT = '''on run argv
activate
display dialog (item 1 of argv) with title "%s" buttons {"取消", "重新搜索"} default button "重新搜索" cancel button "取消"
end run''' % TITLE


def _osa(script, *args):
    r = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True)
    if r.returncode != 0:
        return None          # user pressed cancel / Esc
    return r.stdout.rstrip("\n")


def _state(key, value=None):
    p = os.path.join(data_dir(), "launcher.json")
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if value is None:
        return st.get(key, "")
    st[key] = value
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def _search(query):
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "simple_search_cli.py")
    r = subprocess.run([sys.executable, "-I", script, "search", "--json", "--scope", "all", "-n", "40", *query.split()],
                       capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"files": {"results": []}, "topics": {"results": []}, "notes": [r.stderr.strip()[-300:]]}


def _items(data):
    items, ref = [], {}
    files = (data.get("files") or {}).get("results", [])
    tops = (data.get("topics") or {}).get("results", [])
    for f in files[:40]:
        icon = "📁" if f["is_dir"] else ("🖼" if f["kind"] == "图片" else "📄")
        extra = f" · {f['snippet'][:40]}" if f.get("snippet") else ""
        label = f"{icon} {f['name']}  —  {abbreviate(f['dir'])} · {date_text(f['modified'])}{extra}"
        label = _unique(label, ref)
        items.append(label)
        ref[label] = ("file", f)
    for t in tops[:12]:
        label = _unique(f"💬 {t['title'][:60]}  —  {t['source']} · {date_text(t['modified'])}", ref)
        items.append(label)
        ref[label] = ("topic", t)
    return items, ref


def _unique(label, ref):
    base, n = label, 2
    while label in ref:
        label = f"{base} ({n})"
        n += 1
    return label


def _act_file(f):
    path = f["path"]
    choice = _osa(ACT, f"{f['name']}\n{abbreviate(f['dir'])}", "拷贝路径", "在访达中显示", "打开")
    if choice == "打开":
        actions.open_path(path)
    elif choice == "在访达中显示":
        actions.reveal(path)
    elif choice == "拷贝路径":
        actions.copy(path)


def _act_topic(t):
    choice = _osa(ACT, f"{t['title']}\n{t['source']} · {abbreviate(t.get('project') or '')}", "拷贝继续命令", "在终端继续",
                  "打开会话")
    if choice == "打开会话":
        if t.get("open_url"):
            actions.open_path(t["open_url"])
        elif t.get("resume"):
            actions.resume_in_terminal(t["resume"])
    elif choice == "在终端继续" and t.get("resume"):
        if t["resume"].startswith("open "):
            actions.open_path(t.get("open_url", ""))
        else:
            actions.resume_in_terminal(t["resume"])
    elif choice == "拷贝继续命令" and t.get("resume"):
        actions.copy(t["resume"])


def main():
    if sys.platform != "darwin":
        print("快捷搜索窗口仅支持 macOS；在终端中直接使用 simple-search <查询>。")
        return 1
    prompt = "输入关键词（支持拼音、ext:pdf、dm:week、content:发票、in:~/Downloads…）"
    query = _state("last")
    while True:
        query = _osa(ASK, query or "", prompt)
        if query is None or not query.strip():
            return 0
        query = query.strip()
        _state("last", query)
        data = _search(query)
        items, ref = _items(data)
        if not items:
            msg = f"没有找到「{query}」。"
            notes = [n for n in data.get("notes", []) if n]
            if notes:
                msg += "\n\n" + "\n".join(notes[:2])
            if _osa(ALERT, msg) is None:
                return 0
            continue
        nf = (data.get("files") or {}).get("total", 0)
        nt = (data.get("topics") or {}).get("total", 0)
        head = f"「{query}」 文件 {nf:,} 个 · AI 对话 {nt:,} 个（显示前 {len(items)} 个）"
        picked = _osa(CHOOSE, head, "选择", *items)
        if picked is None:
            return 0
        if picked == "<<again>>":
            continue
        kind, obj = ref.get(picked, (None, None))
        try:
            if kind == "file":
                _act_file(obj)
            elif kind == "topic":
                _act_topic(obj)
        except RuntimeError as e:
            _osa(ALERT, f"操作失败：{e}")
        return 0
