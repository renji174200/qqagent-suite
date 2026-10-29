#!/usr/bin/env bash
# 把插件复制到 AstrBot 插件目录
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "用法: bash scripts/install.sh /path/to/astrbot/data/plugins"
  exit 1
fi
if [ ! -d "$TARGET" ]; then
  echo "目录不存在: $TARGET"
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for d in "$ROOT"/plugins/*/; do
  name="$(basename "$d")"
  echo "安装 $name ..."
  rm -rf "$TARGET/$name"
  cp -r "$d" "$TARGET/$name"
done

echo
echo "完成。请重启 AstrBot："
echo "  docker restart astrbot"
