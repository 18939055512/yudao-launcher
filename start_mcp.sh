#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ ! -f venv/bin/activate ]; then
    echo "请先运行 bash start.sh 创建环境并安装依赖"
    exit 1
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "启动 YudaoLauncher MCP: http://${MCP_HOST:-127.0.0.1}:${MCP_PORT:-5151}/mcp"
exec python mcp_server.py
