# simple-search 使用 SOP

有三种用法，效果一样：

| 方式 | 适合 | 怎么用 |
| --- | --- | --- |
| 搜索窗口 | 日常随手找东西 | 按 **⌥ 空格**，或点菜单栏图标 |
| 终端命令 | 想看完整结果、写脚本 | `simple-search <查询>` |
| 让 AI 帮你搜 | 用自然语言描述 | 在 Claude / Codex 里说「帮我找…」，会自动调用 simple-search 技能 |

## 一、首次设置（约 5 分钟）

在项目根目录运行以下命令。

```sh
./install.sh                 # 安装 simple-search 命令（程序在 ~/.simple-search/app）
simple-search --version               # 如果找不到命令，按 install.sh 的提示把 ~/.local/bin 加进 PATH
simple-search index                   # 建文件名索引：家目录 + 应用程序
simple-search doctor                  # 检查各项能力，应全部显示 ✓
simple-search app install             # 编译并启动搜索窗口（需要 Xcode 命令行工具）
simple-search content index --max-seconds 600   # 建内容索引：桌面、文稿、下载、iCloud 云盘
```

- `simple-search doctor` 里「图片 OCR」显示 ✗ 时，先运行 `xcode-select --install`。
- 内容索引第一次会编译图像识别组件（约 30 秒）。文件多时提示「还剩 N 个未处理」，再运行一次同样的命令即可接着做。
- 代码仓库不需要做内容索引时，可以排除：`simple-search config --set 'content.exclude_paths=["~/Code"]'`。
- 可选：在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里勾选终端，就能搜到邮件附件等受保护的位置。

## 二、搜索窗口

| 操作 | 按键 |
| --- | --- |
| 呼出 / 隐藏 | ⌥ 空格，或点菜单栏图标 |
| 选择结果 | ↑ ↓，翻页用 PageUp / PageDown |
| 切换分类 | ← →（搜索框为空，或光标在开头 / 末尾时）、⇥ / ⇧⇥、⌘1～⌘9 |
| 打开 | ↩ |
| 在访达中显示 | ⌘↩（AI 对话：在终端继续；书签：用默认浏览器打开） |
| 拷贝路径 / 拷贝名称 | ⌘C / ⌥⌘C（AI 对话拷贝的是继续会话的命令，书签拷贝的是链接） |
| 预览 | ⌘Y |
| 立即更新索引 | ⌘R |
| 关闭 | esc |

分类：全部 · 文件夹 · 应用 · 文档 · 图片 · 代码 · AI 对话 · 书签 · 网址。搜索框为空时，「网址」列出已配置的自定义网址，其余分类显示最近的内容。「全部」在文件结果后面附带少量 AI 对话和书签的结果。

在菜单栏点击「更改快捷键…」，选择组合键或双击修饰键。组合键可以选一个或多个 Control、Option、Command、Shift，再选一个普通键，例如 Control + Q。双击可以选择其中一个修饰键，例如双击 Option。

选择时会提示可检测的冲突，保存后立即生效。启动时发现冲突会直接打开修改窗口。双击需要在「系统设置 → 隐私与安全性 → 输入监控」中允许 simple-search。其他应用的共享注册和按键监听可能无法检测。

也可手动修改配置中的 `hotkey`，例如 `"control+q"`、`"command+shift+space"` 或 `"double+option"`，然后点击「重新载入设置」。

不想登录时自动启动：菜单栏图标 → 取消勾选「登录时自动启动」。

### 自定义网址

1. 点击菜单栏图标，选择「自定义网址…」，再点「新增…」。
2. 填写标题、关键词和 HTTP(S) 网址并保存。例如给 `https://github.com/` 设置关键词 `gh`，或给 `https://github.com/search?q={query}` 设置关键词 `ghs`。
3. 在搜索窗口的「网址」或「全部」分类输入 `gh` 或 `ghs Swift 教程`，按回车用系统默认浏览器打开。按 ⌘C 可以拷贝实际链接。

在「自定义网址…」中选中条目即可编辑或删除。修改立即生效，重启后继续使用。关键词为单个词，大小写不同的关键词也视为重复。

输入关键词前缀即可看到候选。例如输入 `gh` 时，`gh` 排在 `ghs` 前面。固定网址候选可直接打开，站内搜索需要输入完整关键词和搜索词。

## 三、终端用法

```sh
simple-search 报告                          # 搜文件名；bg / baogao 用拼音也行
simple-search 'ext:xlsx;xls' dm:lastweek      # 上周改过的 Excel
simple-search in:~/Downloads 'size:>100mb'    # 下载里超过 100M 的文件
simple-search content:削峰填谷 ext:md        # 搜文件内容
simple-search topics MQTT source:codex dm:week   # 搜 AI 对话
simple-search topics show '会话标识'             # 查看某次对话
simple-search bookmarks vpp                 # 搜浏览器书签（书签文件夹名也参与匹配）
simple-search reveal ~/Documents           # 在访达中定位文稿目录
simple-search syntax                        # 完整语法速查
```

加 `--json` 输出结构化结果，加 `--paths` 每行一个路径，加 `-n 50` 改条数。

## 四、维护

| 什么时候 | 做什么 |
| --- | --- |
| 平时 | 不用管。索引超过 30 分钟没更新时，搜索会先用旧索引作答，同时在后台刷新 |
| 刚建的文件搜不到 | `simple-search update`，或在窗口里按 ⌘R |
| 新放了一批文档 / 截图 | `simple-search content index` |
| 插上移动硬盘 | `simple-search index --volumes`，之后拔掉也能搜到（结果标「离线」） |
| 不想索引某个目录 | `simple-search config --set 'exclude_paths=["~/Movies"]'` |
| 需要搜 node_modules / .git 里的东西 | `simple-search config --set include_deps=true`，然后 `simple-search update` |
| 关掉某个对话来源 | `simple-search config --set topics.workbuddy=false` |
| 升级到新版本 | `./install.sh && simple-search app install` |

## 五、常见问题

| 现象 | 处理 |
| --- | --- |
| `simple-search: command not found` | 把 `export PATH="$HOME/.local/bin:$PATH"` 加进 `~/.zshrc`，重开终端 |
| 按快捷键没反应 | 从菜单栏打开「更改快捷键…」查看原因。组合键被占用时可以改用其他键。使用双击时，检查系统的「输入监控」权限，并按系统提示重新打开应用 |
| 窗口打开了但一直「正在加载索引」 | 看 `~/.simple-search/server.log`；确认 `simple-search index` 成功过 |
| 截图里的中文搜不到 | `simple-search doctor` 确认「图片 OCR」为 ✓，然后 `simple-search content index --force --root ~/Desktop` |
| 内容索引太慢 | 先加 `--no-images` 只做文档，或用 `--max-seconds 300` 分批 |
| 想全部重来 | `rm -rf ~/.simple-search/indexes ~/.simple-search/*.sqlite`，然后从第一节重新做 |
| 自检 | `python3 -I ~/.simple-search/app/tests/test_simple_search.py`（应显示 OK） |

## 六、文件位置与卸载

| 内容 | 位置 |
| --- | --- |
| 终端命令 | `~/.local/bin/simple-search` |
| 程序 | `~/.simple-search/app/` |
| 搜索窗口 | `~/Applications/simple-search.app` |
| 索引 | `~/.simple-search/indexes/`、`content.sqlite`、`topics.sqlite` |
| 设置 | `~/.simple-search/config.json` |
| 日志 | `~/.simple-search/server.log`、`app-build.log` |

卸载：`simple-search app uninstall`，然后删除 `~/.simple-search/` 和 `~/.local/bin/simple-search`。
