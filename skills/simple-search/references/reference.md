# simple-search 参考

## JSON 输出结构

`$SIMPLE_SEARCH <查询> --json`（`--scope all` 时 `files` 与 `topics` 同时出现）：

```json
{
 "query": "readme",
 "files": {"total": 2, "elapsed_ms": 0.7, "results": [
   {"name": "readme.md", "path": "<家目录>/Documents/readme.md", "dir": "<家目录>/Documents",
    "kind": "文档", "is_dir": false, "size": 11, "modified": 1792300081, "score": 1093,
    "highlight": [[0, 6]],
    "source": "备份盘", "offline": true,          // 仅外置磁盘结果
    "fuzzy": true,                                // 仅模糊匹配结果
    "snippet": "…命中的【片段】…"}               // 仅内容搜索结果
 ]},
 "topics": {"total": 1, "elapsed_ms": 0.6, "results": [
   {"key": "claude:<uuid>", "source": "Claude Code", "provider": "claude", "title": "…", "project": "/path",
    "modified": 1792300081, "archived": false, "messages": 12, "snippet": "…",
    "resume": "cd '/path' && claude --resume '<uuid>'", "open_url": "claude://…"}
 ]},
 "notes": ["后台刷新、模糊匹配、语法错误等提示"]
}
```

`modified` 为 Unix 秒。`total` 是全部命中数，`results` 受 `-n` 限制。

## 排序规则（relevance）

名称匹配质量：完全相同或去掉扩展名后相同 > 开头匹配 > 单词边界匹配（空格、`-_.([+,`、驼峰、中英文 / 数字交界）> 任意位置；名称越接近关键词越靠前。加分：位于家目录、应用（.app）、文件夹、近期修改（1 天 / 1 周 / 1 月 / 1 年分档）；减分：依赖或缓存目录、应用包内部、隐藏项、目录层级过深。其他排序：`--sort name`（自然排序 A→Z）、`modified`（新→旧）、`size`（大→小）、`path`，`--reverse` 反转。

## 设置项（~/.simple-search/config.json）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `roots` | `[]` | 常驻索引目录；空 = 家目录（macOS 另含 /Applications） |
| `exclude_paths` / `exclude_names` / `exclude_ext` | `[]` | 排除目录、名称、扩展名（扩展名不带点） |
| `include_hidden` | `false` | 深入隐藏目录 |
| `include_deps` | `false` | 深入 node_modules、.git、venv、DerivedData 等 |
| `include_packages` | `false` | 深入 .app 等应用包内部 |
| `include_library` | `false` | 深入 ~/Library |
| `auto_refresh_minutes` | `30` | 超过此时长，搜索时后台刷新 |
| `index_volumes` | `true` | `update` / `index` 时同时更新已连接的外置磁盘 |
| `verify_results` | `true` | 返回前核实文件仍存在 |
| `topics.claude/codex/workbuddy` | `null` | `null` 按安装情况自动；`true`/`false` 手动 |
| `content.roots` | `[]` | 内容索引范围；空 = 桌面、文稿、下载（+ iCloud 云盘） |
| `content.max_doc_mb` | `80` | 超过此大小的文件跳过 |
| `content.max_chars` | `2000000` | 每个文件最多保存的字符数 |
| `content.ocr_images` / `content.image_labels` / `content.ocr_scanned_pdf` | `true` | 图片文字 / 画面标签 / 扫描版 PDF OCR |
| `content.spotlight` | `"auto"` | macOS 上用 Spotlight 补充内容结果；`false` 关闭 |

修改排除规则后，下次更新会自动完整重建该索引。

## 数据与环境变量

- `~/.simple-search/indexes/<目录>-<hash>/`：文件名索引（列式存储，按目录先序排列，任一文件夹的子树是连续区间）。
- `~/.simple-search/content.sqlite`：内容索引（SQLite FTS5 trigram，支持中文子串）。
- `~/.simple-search/topics.sqlite`：AI 对话索引。
- `~/.simple-search/bin/simple-search-vision`：macOS 图像识别组件（自动编译）。
- `SIMPLE_SEARCH_HOME`：改变数据目录。自定义时，在安装和启动前使用同一个绝对路径。`CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`WORKBUDDY_CONFIG_DIR`：各对话来源的数据目录。
- 卸载：删除技能目录和 `~/.simple-search/`。

## AI 对话来源细节

| 来源 | 读取位置 | 说明 |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/*/<uuid>.jsonl`；桌面端 `~/Library/Application Support/Claude/claude-code-sessions/` | 合并桌面描述与 CLI 记录（按 cliSessionId 去重）；只收录用户与助手的文字，忽略工具调用、思考过程和系统提示 |
| Codex | `~/.codex/state_*.sqlite`（最高版本）、`thread_history_*.sqlite`、`sessions/` 与 `archived_sessions/` 下的 JSONL | 优先使用显示名；排除子代理与审查会话 |
| WorkBuddy | `~/.workbuddy/workbuddy.db` 与 `projects/*/*.jsonl` | 仅本地会话、当前账号、未删除；跳过 Quick Ask；按编辑重发后的当前分支收录 |

同步时跳过未变化的文件。来源消失的话题会被移除。

## 性能参考

在 50 万项的目录上（单线程 Python）：建索引约 8 秒，无变化时更新约 2 秒；普通关键词 10–80 ms，拼音 20–60 ms，目录范围内搜索与扩展名 / 类型筛选通常 < 60 ms；单字母这类命中极多的查询约 200 ms。`$SIMPLE_SEARCH bench <查询>` 可在本机测速。
