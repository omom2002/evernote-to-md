#!/bin/sh
# 把本技能安装到智能体的技能目录（默认 WorkBuddy 用户级）。
#
#   ./install.sh                       # 装到 ~/.workbuddy/skills/
#   ./install.sh /path/to/skills       # 装到指定技能目录
#
# 说明：本脚本只做复制，不删除任何东西 —— 目标已存在时先改名备份。
#       只挑需要的文件复制，因此不会带上 __pycache__ 之类的缓存。
set -e

SRC=$(cd "$(dirname "$0")" && pwd)
BASE="${1:-$HOME/.workbuddy/skills}"
DEST="$BASE/evernote-enex-to-md"

mkdir -p "$BASE"

if [ -e "$DEST" ]; then
    BACKUP="$DEST.bak-$(date +%Y%m%d%H%M%S)"
    echo "目标已存在，先备份到：$BACKUP"
    mv "$DEST" "$BACKUP"
fi

mkdir -p "$DEST/scripts"
for f in "$SRC"/SKILL.md "$SRC"/README.md "$SRC"/requirements.txt "$SRC"/compat.json "$SRC"/install.sh; do
    [ -e "$f" ] && cp "$f" "$DEST/"
done
for f in "$SRC"/scripts/*.py "$SRC"/scripts/*.notes; do
    [ -e "$f" ] && cp "$f" "$DEST/scripts/"
done
chmod +x "$DEST"/scripts/*.py 2>/dev/null || true

echo "已安装到：$DEST"
echo
echo "下一步：安装 Python 依赖（只有解密/渲染需要，检查类功能不用装）"
echo "  python3 -m pip install -r \"$DEST/requirements.txt\""
echo
echo "自测（应当输出 2 篇 md + 1 个附件）："
echo "  python3 \"$DEST/scripts/enex_to_md.py\" \"$DEST/scripts/selftest.notes\" \\"
echo "      --password test123 --out /tmp/enex_selftest"
