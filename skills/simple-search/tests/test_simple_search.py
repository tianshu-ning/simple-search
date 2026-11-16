"""在隔离目录验证 simple-search，可用 python3 -I tests/test_simple_search.py 运行。"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "simple_search_cli.py")
TMP = tempfile.mkdtemp(prefix="simple-search-test-")
HOME = os.path.join(TMP, "home")
DATA = os.path.join(TMP, "data")
ENV = dict(os.environ, HOME=HOME, SIMPLE_SEARCH_HOME=DATA)
for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "WORKBUDDY_CONFIG_DIR"):
    ENV.pop(k, None)


def w(rel, text="x", mtime=None):
    p = os.path.join(HOME, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def docx(rel, paragraphs):
    p = os.path.join(HOME, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')


def jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def fixture():
    w("Documents/重庆出差报告.txt", "行程安排")
    w("Documents/工作文档.md", "本季度营收增长，发票编号 INV-8812")
    w("Documents/readme.md")
    w("Documents/README_old.txt", mtime=time.time() - 400 * 86400)
    w("Desktop/IMG_0012.jpg")
    w("Desktop/notes/todo list.txt")
    w("Projects/app/src/MainView.swift")
    w("Projects/app/node_modules/readme/index.js")
    w(".hidden/readme secret.txt")
    w("Downloads/big.bin", "x" * 2_000_000)
    w("Documents/Information/vpp/northbound-dev-platform.html")      # names once wrongly treated as system folders
    w("Documents/System/volume_notes.txt")
    w("Code/svc/pom.xml"); w("Code/svc/src/main/resources/Solis.pem"); w("Code/svc/target/classes/Solis.pem")
    w("Code/game/target/aim.txt")                                       # a plain folder called target
    docx("Documents/合同/采购合同.docx", ["甲方：星河科技有限公司", "付款条件：验收后三十日内"])
    # Claude Code CLI transcript
    cid = str(uuid.UUID(int=11))
    jsonl(os.path.join(HOME, ".claude/projects/-p", cid + ".jsonl"), [
        {"type": "user", "cwd": "/w/p", "message": {"role": "user", "content": "<system-reminder>hidden</system-reminder>优化削峰填谷算法"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "用线性规划"}]}},
    ])
    # Codex
    tid = str(uuid.UUID(int=12))
    rp = os.path.join(HOME, ".codex/sessions/2026/10/01/rollout-" + tid + ".jsonl")
    jsonl(rp, [
        {"type": "session_meta", "payload": {"id": tid, "cwd": "/w/web"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "写一个倒计时组件"}]}},
    ])
    con = sqlite3.connect(os.path.join(HOME, ".codex/state_5.sqlite"))
    con.execute("create table threads(id, title, cwd, updated_at, rollout_path, archived, first_user_message, source)")
    con.execute("insert into threads values(?,?,?,?,?,?,?,?)", (tid, "Countdown", "/w/web", int(time.time()), rp, 0, "", "cli"))
    con.execute("insert into threads values(?,?,?,?,?,?,?,?)", (str(uuid.UUID(int=13)), "归档话题", "/w", int(time.time()), "", 1, "", "cli"))
    con.commit(); con.close()
    # WorkBuddy
    wb = os.path.join(HOME, ".workbuddy")
    os.makedirs(os.path.join(wb, "storage/skeleton"), exist_ok=True)
    with open(os.path.join(wb, "storage/skeleton/account-snapshot.json"), "w") as f:
        json.dump({"version": 1, "uid": "u1", "savedAt": int(time.time() * 1000)}, f)
    con = sqlite3.connect(os.path.join(wb, "workbuddy.db"))
    con.execute("create table sessions(id, cwd, user_id, title, custom_title, status, created_at, updated_at, deleted_at, transport, session_settings)")
    now = int(time.time() * 1000)
    con.executemany("insert into sessions values(?,?,?,?,?,?,?,?,?,?,?)", [
        ("wb1", "/w", "u1", "t", "周报整理", "active", now, now, None, "local", ""),
        ("wb2", "/w", "u2", "别人的会话", "", "active", now, now, None, "local", ""),
        ("wb3", "/w", "u1", "已删除会话", "", "active", now, now, now, "local", ""),
    ])
    con.commit(); con.close()
    jsonl(os.path.join(wb, "projects/k/wb1.jsonl"), [
        {"type": "message", "role": "user", "id": "a", "contentText": "整理巡检周报"},
        {"type": "message", "role": "assistant", "id": "b", "parentId": "a", "contentText": "被替换的旧回答"},
        {"type": "message", "role": "user", "id": "c", "parentId": "a", "contentText": "按电站分组"},
        {"type": "message", "role": "assistant", "id": "d", "parentId": "c", "contentText": "已分组"},
    ])


def web_fixture():
    chrome = os.path.join(HOME, "Library/Application Support/Google/Chrome/Default")
    os.makedirs(chrome, exist_ok=True)
    t = int((time.time() + 11644473600) * 1e6)
    bm = lambda n, u, added=t: {"type": "url", "name": n, "url": u, "date_added": str(added)}
    with open(os.path.join(chrome, "Bookmarks"), "w", encoding="utf-8") as f:
        json.dump({"roots": {"bookmark_bar": {"children": [
            {"type": "folder", "name": "软考", "children": [bm("系统架构设计师真题", "https://example.com/rk")]},
            bm("SolisCloud API 文档", "https://api.soliscloud.com/docs")]}}}, f, ensure_ascii=False)
    with open(os.path.join(chrome, "AccountBookmarks"), "w", encoding="utf-8") as f:
        json.dump({"roots": {"bookmark_bar": {"children": [
            bm("账号里的 API 文档副本", "https://api.soliscloud.com/docs", t + 1_000_000),
            bm("账号同步指南", "https://example.com/sync", t + 2_000_000)]}}}, f, ensure_ascii=False)


def run_cli(*args):
    r = subprocess.run([sys.executable, "-I", SCRIPT, *args], env=ENV, capture_output=True, text=True, timeout=300)
    return r.stdout + r.stderr


def paths(*args):
    out = subprocess.run([sys.executable, "-I", SCRIPT, "--paths", "--no-refresh", *args], env=ENV,
                         capture_output=True, text=True, timeout=300).stdout
    return [os.path.relpath(p, HOME) for p in out.splitlines() if p.startswith("/")]


def topic_titles(*args):
    out = json.loads(run_cli("topics", *args, "--json"))
    return [t["title"] for t in out["topics"]["results"]]


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture()
        # 使用测试目录作为唯一索引根，避免混入本机应用与外置磁盘。
        run_cli("config", "--set", "roots=" + json.dumps([HOME]),
                "--set", "index_volumes=false", "--set", "content.ocr_images=false")
        run_cli("index")

    def test_name_and_rank(self):
        self.assertEqual(paths("readme")[0], "Documents/readme.md")
        self.assertIn("Documents/README_old.txt", paths("readme"))

    def test_hidden_and_deps_not_descended(self):
        self.assertNotIn("Projects/app/node_modules/readme/index.js", paths("index.js"))
        self.assertEqual(paths("secret"), [])

    def test_regular_folders_named_like_system_ones(self):
        self.assertEqual(paths("northbound"), ["Documents/Information/vpp/northbound-dev-platform.html"])
        self.assertEqual(paths("volume_notes"), ["Documents/System/volume_notes.txt"])

    def test_build_output_skipped_only_next_to_build_file(self):
        self.assertEqual(paths("Solis.pem"), ["Code/svc/src/main/resources/Solis.pem"])
        self.assertEqual(paths("aim.txt"), ["Code/game/target/aim.txt"])

    def test_tight_fuzzy_fallback(self):
        self.assertEqual(paths("rdmx"), [])          # scattered letters must not produce junk

    def test_pinyin_polyphonic(self):
        self.assertEqual(paths("cq"), ["Documents/重庆出差报告.txt"])
        self.assertEqual(paths("zq"), ["Documents/重庆出差报告.txt"])
        self.assertEqual(paths("chongqing"), ["Documents/重庆出差报告.txt"])
        self.assertEqual(paths("gzwd"), ["Documents/工作文档.md"])

    def test_operators(self):
        self.assertEqual(set(paths("readme | todo")), {"Documents/readme.md", "Documents/README_old.txt", "Desktop/notes/todo list.txt"})
        self.assertEqual(paths("readme !old"), ["Documents/readme.md"])
        self.assertEqual(paths('"todo list"'), ["Desktop/notes/todo list.txt"])
        self.assertEqual(paths("IMG_????.jpg"), ["Desktop/IMG_0012.jpg"])
        self.assertEqual(paths("~/Desktop/", "txt"), ["Desktop/notes/todo list.txt"])
        self.assertEqual(paths("in:~/Projects", "view"), ["Projects/app/src/MainView.swift"])
        self.assertEqual(paths("ext:docx"), ["Documents/合同/采购合同.docx"])
        self.assertEqual(set(paths("folder:src")), {"Projects/app/src", "Code/svc/src"})
        self.assertEqual(paths("size:>1mb"), ["Downloads/big.bin"])
        self.assertEqual(paths("dm:<2026-01-01", "readme"), ["Documents/README_old.txt"])
        self.assertEqual(paths("regex:^IMG_\\d+"), ["Desktop/IMG_0012.jpg"])
        self.assertEqual(paths("case:README"), ["Documents/README_old.txt"])
        self.assertEqual(paths("kind:nonsense"), [])

    def test_paste_paths(self):
        p = os.path.join(HOME, "Desktop/notes/todo list.txt")
        self.assertEqual(paths("file://" + p.replace(" ", "%20")), ["Desktop/notes/todo list.txt"])
        self.assertEqual(paths(p.replace(" ", "\\ ") + ":12:3"), ["Desktop/notes/todo list.txt"])

    def test_fuzzy_fallback(self):
        self.assertIn("Documents/readme.md", paths("rdme"))

    def test_deleted_filtered_and_update(self):
        w("Desktop/tmp_gone_soon.txt")
        run_cli("update")
        self.assertEqual(paths("gone_soon"), ["Desktop/tmp_gone_soon.txt"])
        os.unlink(os.path.join(HOME, "Desktop/tmp_gone_soon.txt"))
        self.assertEqual(paths("gone_soon"), [])

    def test_content(self):
        out = run_cli("content", "index", "--no-images")
        self.assertIn("完成", out)
        self.assertEqual(paths("content:星河科技"), ["Documents/合同/采购合同.docx"])
        self.assertEqual(paths("content:发票"), ["Documents/工作文档.md"])
        self.assertEqual(paths("content:INV-8812", "ext:md"), ["Documents/工作文档.md"])

    def test_topics(self):
        self.assertEqual(topic_titles("削峰"), ["优化削峰填谷算法"])
        self.assertEqual(topic_titles("倒计时"), ["Countdown"])
        self.assertEqual(topic_titles("巡检"), ["周报整理"])
        self.assertEqual(topic_titles("被替换的旧回答"), [])   # edited branch
        self.assertEqual(topic_titles("别人的会话"), [])       # other account
        self.assertEqual(topic_titles("已删除会话"), [])        # soft-deleted
        self.assertEqual(topic_titles("hidden"), [])            # system tags stripped
        self.assertEqual(topic_titles("归档"), [])
        self.assertEqual(topic_titles("归档", "--archived"), ["归档话题"])
        r = json.loads(run_cli("topics", "削峰", "--json"))["topics"]["results"][0]
        self.assertTrue(r["resume"].endswith("claude --resume " + str(uuid.UUID(int=11))))

    def test_removed_topic_source(self):
        run_cli("topics", "sync")
        key = "deepseek:legacy"
        with sqlite3.connect(os.path.join(DATA, "topics.sqlite")) as con:
            con.execute("PRAGMA user_version=0")
            con.execute("INSERT INTO topics(key, provider, title, modified) VALUES(?, 'deepseek', '旧会话', 0)", (key,))
            con.execute("INSERT INTO msgs(key, role, body) VALUES(?, 'user', '旧会话正文')", (key,))
            con.execute("INSERT INTO cursors(path, key) VALUES('/旧日志', ?)", (key,))

        self.assertEqual(topic_titles("旧会话", "--no-sync"), [])
        self.assertEqual(topic_titles("倒计时", "--no-sync"), ["Countdown"])
        self.assertIn("找不到这个话题", run_cli("topics", "show", key))
        self.assertIn("找不到这个话题", run_cli("topics", "open", key))
        for args in (("search", "source:deepseek"), ("search", "--source", "deepseek"), ("sync", "--source", "deepseek")):
            self.assertIn("不支持的 AI 对话来源：deepseek", run_cli("topics", *args))
        with sqlite3.connect(os.path.join(DATA, "topics.sqlite")) as con:
            self.assertIsNone(con.execute("SELECT key FROM topics WHERE key=?", (key,)).fetchone())
            self.assertIsNone(con.execute("SELECT key FROM msgs WHERE key=?", (key,)).fetchone())
            self.assertIsNone(con.execute("SELECT key FROM cursors WHERE key=?", (key,)).fetchone())

    def test_server_protocol(self):
        run_cli("topics", "sync")
        p = subprocess.Popen([sys.executable, "-I", SCRIPT, "serve"], env=ENV, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        rd = lambda: json.loads(p.stdout.readline())
        self.assertEqual(rd()["op"], "status")
        self.assertEqual(rd()["op"], "ready")
        p.stdin.write(json.dumps({"op": "search", "id": 1, "q": "readme", "tab": "all"}) + "\n"); p.stdin.flush()
        r = rd()
        self.assertEqual(r["items"][0]["name"], "readme.md")
        self.assertEqual(r["items"][0]["hl"], [[0, 6]])
        for i in range(2, 7):
            p.stdin.write(json.dumps({"op": "search", "id": i, "q": "readme"[: i - 1], "tab": "doc"}) + "\n")
        p.stdin.flush()
        self.assertEqual(rd()["id"], 6)          # a burst of keystrokes is answered once
        p.stdin.write(json.dumps({"op": "search", "id": 7, "q": "削峰", "tab": "topics"}) + "\n"); p.stdin.flush()
        hit = rd()["items"][0]
        self.assertEqual(hit["name"], "优化削峰填谷算法")
        self.assertEqual(hit["provider"], "claude")
        p.stdin.write(json.dumps({"op": "topic_copy", "key": hit["key"]}) + "\n")
        p.stdin.flush()
        self.assertTrue(rd()["text"].endswith("claude --resume " + str(uuid.UUID(int=11))))
        p.stdin.write(json.dumps({"op": "search", "id": 8, "q": "source:deepseek", "tab": "all"}) + "\n")
        p.stdin.flush()
        r = rd()
        self.assertEqual(r["items"], [])
        self.assertIn("不支持的 AI 对话来源：deepseek", r["note"])
        p.stdin.close(); p.wait(timeout=10); p.stdout.close()

    def test_bookmarks(self):
        command = [sys.executable, "-I", SCRIPT, "serve"]
        with subprocess.Popen(command, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True) as p:
            try:
                self.assertEqual(json.loads(p.stdout.readline())["op"], "status")
                self.assertEqual(json.loads(p.stdout.readline())["op"], "ready")

                def search(query):
                    p.stdin.write(json.dumps({"op": "search", "q": query, "tab": "bookmarks"}) + "\n")
                    p.stdin.flush()
                    return json.loads(p.stdout.readline())

                for query in ("", "api"):
                    result = search(query)
                    self.assertEqual(result["items"], [])
                    self.assertEqual(result["note"], "没有找到浏览器书签")

                web_fixture()
                result = search("不存在的书签名称")
                self.assertEqual(result["items"], [])
                self.assertEqual(result["note"], "")
                result = search("")
                self.assertEqual(result["total"], 3)
                self.assertTrue(result["recent"])
                self.assertEqual(result["note"], "")
            finally:
                p.stdin.close()
                p.wait(timeout=10)

        out = json.loads(run_cli("bookmarks", "--json"))
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["results"][0]["title"], "账号同步指南")
        out = json.loads(run_cli("bookmarks", "rk", "--json"))           # 书签文件夹的拼音首字母
        self.assertEqual([r["title"] for r in out["results"]], ["系统架构设计师真题"])
        out = json.loads(run_cli("bookmarks", "api", "--json"))
        self.assertEqual([r["title"] for r in out["results"]], ["SolisCloud API 文档"])
        self.assertEqual(out["results"][0]["url"], "https://api.soliscloud.com/docs")
        out = json.loads(run_cli("bookmarks", "同步指南", "--json"))
        self.assertEqual([r["url"] for r in out["results"]], ["https://example.com/sync"])

    def test_web_shortcuts(self):
        entries = [
            {"title": "相关工具", "keyword": "readme-tools", "url": "https://example.com/tools"},
            {"title": "固定网址", "keyword": "readme", "url": "https://example.com/"},
            {"title": "重复条目", "keyword": "README", "url": "https://duplicate.example/"},
            {"title": "站内搜索", "keyword": "查找", "url": "https://example.com/search?q={query}"},
            {"title": "无效协议", "keyword": "local", "url": "file:///tmp/example"},
            {"title": "无效占位符", "keyword": "host", "url": "https://{query}.example.com/"},
        ]
        config_path = os.path.join(DATA, "config.json")
        with open(config_path, "rb") as f:
            original = f.read()

        def save(entries):
            config = json.loads(original)
            config["web_shortcuts"] = entries
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False)

        command = [sys.executable, "-I", SCRIPT, "serve"]
        try:
            with subprocess.Popen(command, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True) as p:
                try:
                    self.assertEqual(json.loads(p.stdout.readline())["op"], "status")
                    self.assertEqual(json.loads(p.stdout.readline())["op"], "ready")

                    def search_result(query, tab="all", limit=80, reload=False):
                        if reload:
                            p.stdin.write('{"op":"reload_web_shortcuts"}\n')
                        p.stdin.write(json.dumps({"op": "search", "q": query, "tab": tab, "limit": limit}) + "\n")
                        p.stdin.flush()
                        return json.loads(p.stdout.readline())

                    def search(*args, **kwargs):
                        return search_result(*args, **kwargs)["items"]

                    self.assertEqual(search("readme")[0]["t"], "file")
                    empty = search_result("", tab="web_shortcuts")
                    self.assertEqual(empty["items"], [])
                    self.assertEqual(empty["note"], "还没有配置自定义网址")
                    save(entries)
                    items = search("README", reload=True)
                    self.assertEqual(items[0]["path"], "https://example.com/")
                    self.assertEqual(items[1]["r1"], "readme-tools")
                    self.assertEqual(items[2]["name"], "readme.md")
                    prefix = search("READM")
                    self.assertEqual([item["r1"] for item in prefix if item["t"] == "web_shortcut"],
                                     ["readme", "readme-tools"])
                    self.assertEqual(prefix[0]["path"], "https://example.com/")
                    self.assertEqual(len(search("readme", limit=1)), 1)
                    self.assertEqual(search("readm", tab="doc")[0]["t"], "file")
                    self.assertEqual(search("查找 中文 A&B? #+/%")[0]["path"],
                                     "https://example.com/search?q=%E4%B8%AD%E6%96%87%20A%26B%3F%20%23%2B%2F%25")
                    for query in ("查", "查找  "):
                        empty_query = search(query)[0]
                        self.assertEqual(empty_query["path"], "")
                        self.assertIn("查找 搜索词", empty_query["sub"])
                    for query in ("", "eadme", "查 中文", "local", "host example"):
                        self.assertNotIn("web_shortcut", [item["t"] for item in search(query)])

                    web = search_result("", tab="web_shortcuts", limit=2)
                    self.assertEqual(web["total"], 3)
                    self.assertEqual([item["r1"] for item in web["items"]], ["readme", "readme-tools"])
                    self.assertEqual([item["r1"] for item in search("READM", tab="web_shortcuts")],
                                     ["readme", "readme-tools"])
                    self.assertEqual(search("readme.md", tab="web_shortcuts"), [])
                    self.assertEqual(search("查找", tab="web_shortcuts")[0]["path"], "")
                    self.assertEqual(search("查找 中文 A&B? #+/%", tab="web_shortcuts")[0]["path"],
                                     "https://example.com/search?q=%E4%B8%AD%E6%96%87%20A%26B%3F%20%23%2B%2F%25")
                    save([])
                    self.assertEqual(search("readme", reload=True)[0]["t"], "file")
                finally:
                    p.stdin.close()
                    p.wait(timeout=10)

            save(entries)
            restarted = subprocess.run(command, env=ENV, text=True, capture_output=True, check=True, timeout=10,
                                       input='{"op":"search","q":"readme","tab":"all"}\n')
            result = json.loads(restarted.stdout.splitlines()[-1])
            self.assertEqual(result["items"][0]["path"], "https://example.com/")
        finally:
            with open(config_path, "wb") as f:
                f.write(original)

    def test_offline_volume(self):
        vol = os.path.join(TMP, "vol")
        os.makedirs(os.path.join(vol, "photos"))
        w_path = os.path.join(vol, "photos", "travel_2019.jpg")
        open(w_path, "w").close()
        code = ("import sys; sys.path.insert(0, %r); from simple_search import indexer, common; "
                "indexer.refresh(%r, common.load_config(), extra_meta={'volume': True, 'label': 'Backup'}, quiet=True)"
                % (os.path.dirname(SCRIPT), vol))
        subprocess.run([sys.executable, "-I", "-c", code], env=ENV, check=True)
        shutil.rmtree(vol)
        out = json.loads(run_cli("travel", "--json", "--no-refresh"))
        hit = out["files"]["results"][0]
        self.assertTrue(hit.get("offline"))
        self.assertEqual(hit["source"], "Backup")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
