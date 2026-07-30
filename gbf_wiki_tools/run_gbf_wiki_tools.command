#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT_DIR/app/launcher.py"
fi

if command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT_DIR/app/launcher.py"
fi

echo "未找到 Python，请先安装 Python 3。"
echo
read -r -p "按回车键退出..." _
exit 1
