---
name: simple-search
description: simple-search 是本机搜索工具。 本机极速搜索：按文件名秒搜整台电脑（拼音含多音字、通配符、正则、按类型/大小/日期/目录筛选），搜文件内容（PDF、Word、PPT、Excel、Pages、Markdown，以及截图和照片里的文字与画面），搜外置磁盘（拔掉后仍可搜），并搜索本机 Claude Code、Codex、WorkBuddy 的历史对话，一键打开或继续会话。只要用户想在自己电脑上「找文件 / 找某个文档 / 找截图 / 找含某句话的文件 / 找以前和 AI 聊过的某次对话」，或提到「在哪个文件夹」「最近下载的 PDF」「上周改过的 Excel」「拼音搜文件」，都使用本技能。所有处理都在本机完成，不上传。
---

# simple-search

一条命令搜遍本机：文件名、文件内容、外置磁盘、AI 对话。索引只存在本机 `~/.simple-search/`。

```
SIMPLE_SEARCH="python3 -I <本技能目录>/scripts/simple_search_cli.py"
```

`<本技能目录>` 是这份 SKILL.md 所在的目录。macOS 与 Linux 均可运行，只需 Python 3.8+，无需安装第三方包。

## 先确定环境

本技能要读的是**用户自己电脑上的文件**。如果你运行在隔离沙箱 / 虚拟机里（例如只挂载了部分文件夹），先用 `ls ~` 判断能看到什么：
- 看得到用户的家目录 → 直接搜。
- 只挂载了某个文件夹 → 用 `--in <挂载路径>` 只搜那里，并告诉用户如需全盘搜索要在本机终端运行，或授权更多文件夹。
- `open` / `reveal` / `preview` / 继续 AI 会话 需要图形界面，在沙箱里会失败；此时把路径或命令给用户即可。

## 核心用法

```
$SIMPLE_SEARCH <查询>                 # 搜文件名（首次会自动建索引，之后毫秒级）
$SIMPLE_SEARCH <查询> --json          # 结构化结果（推荐给你自己解析）
$SIMPLE_SEARCH <查询> --paths         # 每行一个路径
$SIMPLE_SEARCH --scope all <查询>     # 文件 + AI 话题一起搜
$SIMPLE_SEARCH app install            # 安装 macOS 原生搜索窗口（菜单栏常驻、全局热键 Option + 空格，边输入边出结果）
$SIMPLE_SEARCH topics <关键词>        # 只搜 AI 对话
$SIMPLE_SEARCH bookmarks <关键词>     # 搜浏览器书签（Chrome/Edge/Brave，书签文件夹名也参与匹配）
$SIMPLE_SEARCH content index          # 建立内容索引（首次需几分钟，之后增量）
$SIMPLE_SEARCH content:<词> [其他条件] # 搜文件内容
```

常用选项：`-n 50`（条数，默认 30）、`--in ~/Documents`（限定目录，可多次）、`--sort modified|size|name|path`、`--reverse`、`--refresh`（先更新索引再搜）。

### 搜索语法（可组合，空格 = 且）

| 写法 | 含义 |
| --- | --- |
| `wd`、`wendang`、`cq`/`zq` | 拼音首字母或全拼，含多音字（「重庆」用 cq、zq 都能搜到） |
| `foo bar` / `foo \| bar` | 同时包含 / 包含其一 |
| `readme !node_modules` | 排除名称或上级目录含 node_modules 的 |
| `"my notes"` | 引号内为一个整体 |
| `*.png`、`IMG_????.jpg` | 通配符，匹配整个名称；含 `/` 时匹配完整路径 |
| `~/Desktop/ png`、`in:~/Desktop` | 只在某目录下（毫秒级的子树范围） |
| `path:src/utils` | 路径包含 |
| `ext:pdf;docx` | 扩展名 |
| `kind:image` | folder app doc image video audio code archive（中文「图片」「文档」也可） |
| `file:` / `folder:`、`file:报告` | 只要文件 / 只要文件夹 |
| `size:>10mb`、`size:1mb..5mb`、`size:huge` | 大小 |
| `dm:today` `dm:yesterday` `dm:week` `dm:lastweek` `dm:month` `dm:7d` `dm:12h` `dm:2026-10` `dm:2026-01..2026-03` `dm:>2026-06` | 修改时间 |
| `regex:^IMG_\d+` / `case:README` / `fuzzy:rdme` | 正则 / 区分大小写 / 模糊 |
| `content:发票`、`content:"付款 条件"` | 文件内容（需先 `content index`） |

用户直接粘贴的路径（`file://` 链接、带引号或反斜杠转义、末尾 `:行:列`）可以原样作为查询。完整速查：`$SIMPLE_SEARCH syntax`。

把用户的自然语言翻译成语法，例如：
- 「上周改过的 Excel」→ `ext:xlsx;xls dm:lastweek`
- 「下载里超过 100M 的视频」→ `in:~/Downloads kind:video size:>100mb`
- 「含有星河科技的合同」→ `content:星河科技 合同`（不确定文件名就只用 `content:星河科技`）
- 「之前和 Claude 聊过的 MQTT 订阅」→ `topics MQTT 订阅 source:claude`

### 文件内容搜索（PDF / Office / 图片文字）

```
$SIMPLE_SEARCH content index                    # 默认范围：桌面、文稿、下载（macOS 另含 iCloud 云盘）
$SIMPLE_SEARCH content index --root ~/Work      # 指定范围（可多次）
$SIMPLE_SEARCH content index --max-seconds 120  # 分批建立，再次运行会接着处理
$SIMPLE_SEARCH content status
```

- 支持：txt/md/代码/csv/html、PDF（含扫描版 OCR）、docx/pptx/xlsx、odt/ods/odp、epub、Pages/Numbers/Keynote、doc/rtf；图片（png/jpg/heic/webp…）的文字与画面（如「猫」「海滩」「截图」）。
- macOS 用系统自带 Vision 与 PDFKit（首次会自动编译一个小组件，约 30 秒，需要 Xcode 命令行工具）；没有时回退到 `pdftotext` / `tesseract`。macOS 上还会用 Spotlight 补充结果。
- 建索引较慢时，先告诉用户预计耗时，或用 `--max-seconds` 分批；只需文档时加 `--no-images`。
- 内容搜索可以和其他条件组合：`content:发票 ext:pdf dm:month`、`content:roadmap kind:image`。

### AI 对话搜索

```
$SIMPLE_SEARCH topics 削峰填谷                    # 搜标题与对话正文
$SIMPLE_SEARCH topics MQTT source:codex dm:week  # 限定来源 / 时间；project:<路径片段> 限定项目
$SIMPLE_SEARCH topics 储能 --archived            # 包含已归档
$SIMPLE_SEARCH topics show <key>                 # 查看这次对话的正文
$SIMPLE_SEARCH topics open <key> [--terminal]    # 打开原应用；--terminal 在终端继续 CLI 会话
```

来源会自动识别，包括 Claude Code（CLI 与桌面端 Code 会话）、Codex 和 WorkBuddy。搜索只读本机已保存的数据。结果按来源提供 `resume` 或 `open_url`，并带有供 `topics show` 使用的 `key`。

### 对结果做操作

```
$SIMPLE_SEARCH open <路径>      $SIMPLE_SEARCH reveal <路径>     $SIMPLE_SEARCH preview <路径>
$SIMPLE_SEARCH copy <路径> [--name]                    $SIMPLE_SEARCH trash <路径>
```

`trash` 只移到废纸篓（可恢复），**执行前必须得到用户明确同意**；不要用 `rm` 删除用户文件。

### 索引与外置磁盘

```
$SIMPLE_SEARCH update                      # 快速更新全部索引（只重扫变化过的文件夹）
$SIMPLE_SEARCH index --add ~/Work          # 把目录加入常驻索引范围
$SIMPLE_SEARCH index --volumes             # 索引已连接的外置磁盘；拔掉后仍可搜（结果标「离线」）
$SIMPLE_SEARCH index --list                # 查看所有索引
$SIMPLE_SEARCH status / doctor             # 状态 / 能力检测
```

- 默认索引家目录（macOS 另含 /Applications）。依赖目录（node_modules、.git…）、隐藏目录、应用包内部、~/Library 只列出名字不深入；可在设置里打开。
- 搜索时如果索引超过 30 分钟未更新，会立即用现有索引作答并在后台刷新；需要最新结果加 `--refresh`。结果在返回前会核实是否仍存在，刚删除的文件不会出现。
- `--in` 指向未被索引的目录时，会自动建立并缓存一个临时索引。

### 设置

```
$SIMPLE_SEARCH config                                   # 查看
$SIMPLE_SEARCH config --set include_hidden=true         # 深入隐藏目录
$SIMPLE_SEARCH config --set include_deps=true           # 深入 node_modules 等依赖目录
$SIMPLE_SEARCH config --set 'exclude_paths=["~/Movies"]'
$SIMPLE_SEARCH config --set 'exclude_ext=["log","tmp"]'
$SIMPLE_SEARCH config --set topics.workbuddy=false      # 关闭某个对话来源（null = 自动）
$SIMPLE_SEARCH config --set content.ocr_images=false
$SIMPLE_SEARCH config --set 'content.exclude_paths=["~/Documents/Code"]'   # 只在内容索引中跳过（如代码仓库）
```

完整设置项见 `references/reference.md`。

## 回答用户时

- 先给结论：最可能的 1–3 个结果写出名称、所在文件夹（用 `~` 缩写）、修改时间；其余可折叠或按需列出。
- 结果太多时主动收窄（加 `ext:`、`dm:`、`in:`），没有结果时依次尝试：拼音 / 更短的关键词 / `fuzzy:` / `content:` / `--refresh`。
- 内容命中要附上片段（`snippet`），话题结果附上 `resume` 命令。
- 出现「提示：」行时（如后台刷新、未建内容索引、外置磁盘未索引），把相关信息转告用户。

## 疑难

- 搜不到刚创建的文件 → `--refresh` 或 `$SIMPLE_SEARCH update`。
- macOS 搜不到「邮件」等受保护目录 → 给运行 Python 的终端授予「完全磁盘访问权限」（可选）。
- 图片内容为空 → `$SIMPLE_SEARCH doctor` 检查 OCR；macOS 需 `xcode-select --install`，Linux 需 `tesseract`（中文需 `tesseract-ocr-chi-sim`）。
- 自检：`python3 -I <本技能目录>/tests/test_simple_search.py`（使用临时目录，不影响真实索引）。
