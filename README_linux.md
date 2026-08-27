# YudaoLauncher (Linux 版)

在 Linux 服务器上一键启动的「项目管理器」（Flask 后端 + 原生前端）。

## 运行

```bash
# 1. 解压后进入目录
tar xzf yudao_launcher-linux.tar.gz
cd yudao_launcher

# 2. 启动（自动创建 venv 并安装依赖，首次稍慢）
bash start.sh
```

启动后浏览器访问：`http://<服务器IP>:5150`

## 自定义

```bash
bash start.sh --host 0.0.0.0 --port 8080   # 指定监听地址/端口
bash start.sh --no-browser                  # 不自动打开浏览器（Linux 无桌面时默认不打开）
```

也可在 `config.json` 的 `server` 中写死 `host` / `port`，优先级低于命令行参数。

## 后台运行（可选）

```bash
nohup bash start.sh > launcher.out 2>&1 &
# 停止：找到进程 kill 掉，或按 Ctrl+C（前台运行时）
```

## 说明

- 首次启动会自动生成 `config.json`（含 yudao/bootdo/ruoyi 三套默认框架）。
- 远程打包依赖服务器开启 SSH/SFTP，且远端已安装 JDK/Maven。
- 前端直接由 Flask 提供（`templates/index.html`），无需额外构建。
