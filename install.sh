#!/bin/sh
# 安装 simple-search 命令和本地程序文件。
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/skills/simple-search"
DATA="${SIMPLE_SEARCH_HOME:-$HOME/.simple-search}"
APP="$DATA/app"
BIN="$HOME/.local/bin"

[ -f "$SRC/scripts/simple_search_cli.py" ] || { echo "找不到 $SRC，请在仓库根目录运行。" >&2; exit 1; }
mkdir -p "$DATA" "$BIN"
chmod 700 "$DATA"
rm -rf "$APP.new"
cp -R "$SRC" "$APP.new"
cp "$SRC/../../docs/SOP.md" "$SRC/../../LICENSE" "$APP.new/"
find "$APP.new" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$APP"
mv "$APP.new" "$APP"

cat > "$BIN/simple-search" <<'WRAP'
#!/bin/sh
# 用与搜索窗口一致的数据目录定位命令行入口。
for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  command -v "$PY" >/dev/null 2>&1 && break
done
exec "$PY" -I "${SIMPLE_SEARCH_HOME:-$HOME/.simple-search}/app/scripts/simple_search_cli.py" "$@"
WRAP
chmod 755 "$BIN/simple-search"

echo "已安装：$BIN/simple-search（程序在 $APP）"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "提示：$BIN 不在 PATH 里，运行下面这行后重开终端："
     echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
esac
echo "下一步：simple-search index，然后 simple-search app install（macOS 搜索窗口）"
