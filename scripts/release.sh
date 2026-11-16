#!/bin/zsh
# 构建本地 macOS 压缩包，显式指定 --publish 时发布到当前仓库。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL="$ROOT/skills/simple-search"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SKILL/scripts/simple_search/common.py")"
BUNDLE_ID="$(sed -n 's/^BUNDLE_ID = "\(.*\)"/\1/p' "$SKILL/scripts/simple_search/appinstall.py")"
DIST="$ROOT/dist"
APP="$DIST/simple-search.app"
ZIP="$DIST/simple-search-macOS.zip"

command -v swiftc >/dev/null || { echo "需要 Xcode 命令行工具：xcode-select --install" >&2; exit 1; }
[ -n "$BUNDLE_ID" ] || { echo "找不到应用标识。" >&2; exit 1; }
rm -rf "$DIST"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "编译搜索窗口（Apple 芯片 + Intel，约 2~4 分钟）…"
SRC="$SKILL/scripts/helper/SimpleSearchApp.swift"
BIN="$APP/Contents/MacOS/SimpleSearchBin"
if swiftc -O -target arm64-apple-macos11 -o "$DIST/arm64" "$SRC" && \
   swiftc -O -target x86_64-apple-macos11 -o "$DIST/x86_64" "$SRC"; then
  lipo -create "$DIST/arm64" "$DIST/x86_64" -output "$BIN"
else
  echo "通用二进制失败，只编译本机架构。"
  swiftc -O -target "$(uname -m)-apple-macos11" -o "$BIN" "$SRC"
fi
rm -f "$DIST/arm64" "$DIST/x86_64"

# 首次运行时将程序和使用说明安装到数据目录。
cp -R "$SKILL" "$APP/Contents/Resources/app"
cp "$ROOT/docs/SOP.md" "$ROOT/LICENSE" "$APP/Contents/Resources/app/"
find "$APP/Contents/Resources/app" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "$VERSION" > "$APP/Contents/Resources/app/VERSION"
cp "$SKILL/data/AppIcon.icns" "$SKILL/data/StatusIcon.png" "$APP/Contents/Resources/"

cat > "$APP/Contents/MacOS/SimpleSearch" <<'LAUNCH'
#!/bin/zsh
# 首次运行时安装程序和命令，再启动原生窗口。
HERE="${0:A:h}"
RES="$HERE/../Resources"
DATA="${SIMPLE_SEARCH_HOME:-$HOME/.simple-search}"
note() { osascript -e "display notification \"$1\" with title \"simple-search\"" >/dev/null 2>&1; }

PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do [ -x "$p" ] && PY="$p" && break; done
if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1; then PY=/usr/bin/python3; fi
if [ -z "$PY" ]; then
  osascript -e 'display dialog "simple-search 需要 Python 3。\n\n点「安装」会打开苹果的「命令行开发者工具」安装窗口（免费，约几分钟），装好后再打开 simple-search。" buttons {"取消", "安装"} default button "安装" with title "simple-search"' >/dev/null 2>&1 \
    && xcode-select --install >/dev/null 2>&1
  exit 0
fi

mkdir -p "$DATA" "$HOME/.local/bin"; chmod 700 "$DATA"
if [ "$(cat "$DATA/app/VERSION" 2>/dev/null)" != "$(cat "$RES/app/VERSION")" ]; then
  rm -rf "$DATA/app.new" && cp -R "$RES/app" "$DATA/app.new" && rm -rf "$DATA/app" && mv "$DATA/app.new" "$DATA/app"
  [ -f "$DATA/index-built" ] || note "首次启动：正在建立索引，通常十几秒到一分钟…"
fi
cat > "$HOME/.local/bin/simple-search" <<WRAP
#!/bin/sh
exec "$PY" -I "\${SIMPLE_SEARCH_HOME:-\$HOME/.simple-search}/app/scripts/simple_search_cli.py" "\$@"
WRAP
chmod 755 "$HOME/.local/bin/simple-search"
touch "$DATA/index-built"
exec "$HERE/SimpleSearchBin" "$@"
LAUNCH
chmod 755 "$APP/Contents/MacOS/SimpleSearch"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>simple-search</string>
  <key>CFBundleDisplayName</key><string>simple-search</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>SimpleSearch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
( cd "$DIST" && ditto -c -k --keepParent "simple-search.app" "$ZIP" )
echo "已生成：$ZIP（$(du -h "$ZIP" | cut -f1)）"

if [ "$1" = "--publish" ]; then
  TAG="v$VERSION"
  gh release view "$TAG" >/dev/null 2>&1 && gh release upload "$TAG" "$ZIP" --clobber \
    || gh release create "$TAG" "$ZIP" --title "simple-search $VERSION" --notes "下载 simple-search-macOS.zip，解压后把 simple-search 拖进「应用程序」再打开。首次打开需在「系统设置 → 隐私与安全性」点「仍要打开」。"
fi
