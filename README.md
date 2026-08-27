# YudaoLauncher

一个用于管理本地及远程开发项目的轻量启动器，提供项目启动、停止、编译、日志、
打包和状态查看功能，并可通过 MCP 让 Codex 等 Agent 调用常用操作。

## 安装

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Linux:   source venv/bin/activate
pip install -r requirements.txt
```

首次使用请复制示例配置，随后只修改本机的 `config.json`：

```bash
cp config.example.json config.json
```

Windows CMD 可使用：

```bat
copy config.example.json config.json
```

`config.json` 可能包含 SSH 凭据和个人路径，已被 Git 忽略，禁止提交。

## 启动

```bash
python app.py --host 127.0.0.1 --port 5150
```

浏览器访问 `http://127.0.0.1:5150`。Linux 部署参见
[README_linux.md](README_linux.md)。

## MCP

```bash
pip install -r requirements_mcp.txt
python mcp_server.py
```

默认 MCP 地址为 `http://127.0.0.1:5151/mcp`。完整配置与局域网部署说明参见
[README_MCP.md](README_MCP.md)。

## 安全

- 不要提交 `config.json`、备份配置、日志、截图或打包产物。
- Launcher 与 MCP 均可触发项目命令，默认只应监听回环地址。
- 局域网部署时使用防火墙限制来源 IP；公网部署前必须增加 HTTPS、认证与访问控制。
