# YudaoLauncher MCP

此 MCP 服务把现有 Launcher API 封装成 Agent 可调用的 tools：

- `list_projects`：列出项目和状态
- `compile_project`：按名称或 ID 异步编译项目
- `start_project`：按名称或 ID 启动项目
- `open_project`：返回项目的局域网访问 URL
- `project_status`：查询运行及编译状态

## Linux 启动

先启动 Launcher：

```bash
bash start.sh --no-browser
```

安装 MCP 依赖并启动（默认只监听本机）：

```bash
source venv/bin/activate
pip install -r requirements_mcp.txt
LAUNCHER_PUBLIC_HOST=192.168.1.100 bash start_mcp.sh
```

本机 MCP 地址：`http://127.0.0.1:5151/mcp`。

可信局域网中的 Agent 需要直连时，可显式监听所有网卡：

```bash
MCP_HOST=0.0.0.0 \
LAUNCHER_PUBLIC_HOST=192.168.1.100 \
bash start_mcp.sh
```

Agent 地址：`http://192.168.1.100:5151/mcp`。请把示例 IP 换成 Linux
服务器的固定局域网 IP。

可配置环境变量：

- `LAUNCHER_URL`：Launcher API，默认 `http://127.0.0.1:5150`
- `LAUNCHER_PUBLIC_HOST`：项目 URL 使用的服务器 IP 或域名
- `MCP_HOST`：MCP 监听地址，默认安全值 `127.0.0.1`
- `MCP_PORT`：MCP 端口，默认 `5151`

## 网络与安全

同一局域网使用不需要外网。Linux 防火墙应只向可信设备放行 TCP 5150、
5151 和各项目自身端口。MCP 能触发配置中的启动及编译命令，因此不要把 5150/5151
直接暴露公网。公网场景应放在带 HTTPS、身份认证和访问控制的反向代理之后。

`open_project` 不会在 Linux 服务器上弹浏览器，而是把 URL 返回给 Agent；Agent
或用户应在自己的设备上打开该 URL。
