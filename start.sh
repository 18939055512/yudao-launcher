#!/usr/bin/env bash
# YudaoLauncher Linux 启动脚本
# 用法:
#   bash start.sh                 # 默认监听 0.0.0.0:5150
#   bash start.sh --host 0.0.0.0 --port 8080
#   bash start.sh --no-browser
set -e

# 切换到脚本所在目录，保证能找到 app.py 与 templates
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，请先安装 Python 3.9+"
    exit 1
fi

# 首次运行自动创建虚拟环境并安装依赖
# 若 venv 目录存在但 activate 缺失（上次创建失败残留），则重建
if [ ! -f venv/bin/activate ]; then
    if [ -e venv ]; then
        echo "检测到不完整的 venv，删除后重建 ..."
        rm -rf venv
    fi
    echo "创建虚拟环境 venv ..."
    if ! python3 -m venv venv; then
        echo "venv 创建失败，请先安装 python3-venv，例如:"
        echo "  sudo apt install python3-venv   # 或对应版本 python3.x-venv"
        exit 1
    fi
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "安装/更新依赖 ..."
pip install -q -U pip
pip install -q -r requirements.txt

echo "启动 YudaoLauncher ..."
exec python app.py "$@"
