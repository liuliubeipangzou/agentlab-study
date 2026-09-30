#!/bin/zsh -f
# 双击此文件，在项目目录启动本地学习界面；已有服务会直接打开浏览器。
set -eu
AGENTLAB_DIRECTORY="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$AGENTLAB_DIRECTORY"
if ! command -v python3 >/dev/null 2>&1; then
    print -u2 '未找到 python3。请先安装 Python 3.9 或更新版本。'
    exit 1
fi
exec python3 -m agentlab.launcher
