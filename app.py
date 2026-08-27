"""
项目启动器 - Project Launcher
管理任意多个项目的启动、停止和状态监控
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import atexit
import signal
import threading
import locale
import glob
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import paramiko
from flask import Flask, render_template, request, jsonify

# ============================================================
# 配置
# ============================================================
# PyInstaller 打包后 __file__ 指向临时解压目录，配置会丢失
# 需要检测 frozen 状态，使用 exe 所在目录
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# --- 预置框架（框架默认命令）。可在界面增删改 ---
DEFAULT_FRAMEWORKS = [
    {
        "id": "yudao",
        "name": "yudao",
        "display_name": "芋道 Yudao",
        "icon": "🍃",
        "startCommand": "mvn exec:java -f yudao-server/pom.xml -Dexec.mainClass=cn.iocoder.yudao.server.YudaoServerApplication -Dspring.profiles.active=local",
        "compileCommand": "mvn clean install -DskipTests",
        "packageCommand": "mvn clean package -DskipTests",
        "artifactPath": "yudao-server/target/*.jar"
    },
    {
        "id": "bootdo",
        "name": "bootdo",
        "display_name": "BootDo",
        "icon": "🌱",
        "startCommand": "mvn spring-boot:run",
        "compileCommand": "mvn clean install -DskipTests",
        "packageCommand": "mvn clean package -DskipTests",
        "artifactPath": "target/*.jar"
    },
    {
        "id": "ruoyi",
        "name": "ruoyi",
        "display_name": "RuoYi 若依",
        "icon": "🍊",
        "startCommand": "mvn spring-boot:run",
        "compileCommand": "mvn clean install -DskipTests",
        "packageCommand": "mvn clean package -DskipTests",
        "artifactPath": "target/*.jar"
    }
]

DEFAULT_CONFIG = {
    "projects": [],
    "remoteServers": [],
    "remoteProjects": [],
    "frameworks": DEFAULT_FRAMEWORKS,
    "server": {
        "port": 5150,
        "host": "0.0.0.0",
    }
}


def get_framework(framework_id):
    """按 id 或 name 查找框架配置，找不到返回 None"""
    if not framework_id:
        return None
    for fw in load_config().get("frameworks", []):
        if fw.get("id") == framework_id or fw.get("name") == framework_id:
            return fw
    return None


def resolve_command(project, framework, proj_key, fw_key):
    """命令解析：项目自定义命令非空优先，否则回退框架默认命令。
    返回 (最终命令, 来源 'project'/'framework'/None)"""
    proj_cmd = (project.get(proj_key) or "").strip() if isinstance(project, dict) else ""
    if proj_cmd:
        return proj_cmd, "project"
    if framework:
        fw_cmd = (framework.get(fw_key) or "").strip()
        if fw_cmd:
            return fw_cmd, "framework"
    return "", None


def resolve_artifact_path(project, framework):
    """产物路径解析：项目级 artifactPath 优先，否则框架级"""
    proj_path = (project.get("artifactPath") or "").strip() if isinstance(project, dict) else ""
    if proj_path:
        return proj_path
    if framework:
        fw_path = (framework.get("artifactPath") or "").strip()
        if fw_path:
            return fw_path
    return ""


def migrate_old_config(data):
    """自动迁移旧版格式 (backend/frontend 键) 到新版 (projects 数组)"""
    if "projects" in data:
        return data
    if "backend" not in data and "frontend" not in data:
        return data

    projects = []
    order = 1
    if "backend" in data:
        projects.append({
            "id": "yudao-backend",
            "name": "芋道后端",
            "path": data["backend"].get("path", ""),
            "command": data["backend"].get("command", "mvn spring-boot:run"),
            "port": data["backend"].get("port", 48080),
            "enabled": True,
            "startOrder": order,
        })
        order += 1
    if "frontend" in data:
        projects.append({
            "id": "yudao-frontend",
            "name": "芋道前端",
            "path": data["frontend"].get("path", ""),
            "command": data["frontend"].get("command", "npm run local"),
            "port": data["frontend"].get("port", 3000),
            "enabled": True,
            "startOrder": order,
        })
        order += 1

    return {
        "projects": projects,
        "server": data.get("server", {"port": 5150}),
    }


def _safe_id(text):
    """生成安全的项目 ID（英文、数字、连字符）"""
    # 拼音简拼 or 直接英文
    tid = text.strip().lower()
    tid = re.sub(r'[^\w\-]', '-', tid)
    tid = re.sub(r'-+', '-', tid)
    return tid.strip('-') or "project"


# ============================================================
# 进程管理器
# ============================================================
# 获取系统编码用于子进程输出（Windows 通常为 GBK/cp936）
_SYS_ENCODING = locale.getpreferredencoding()

class ProcessManager:
    """动态管理所有项目子进程"""

    def __init__(self):
        self._lock = threading.Lock()
        self._processes = {}   # {project_id: Popen}
        self._log_files = {}   # {project_id: Path}

    def _log_path(self, project_id):
        if project_id not in self._log_files:
            self._log_files[project_id] = LOGS_DIR / f"{project_id}.log"
        return self._log_files[project_id]

    def _find_project(self, project_id):
        """在配置中查找项目"""
        config = load_config()
        for p in config["projects"]:
            if p["id"] == project_id:
                return p
        return None

    def get_status(self, project_id):
        """获取进程状态: stopped | starting | running | error"""
        with self._lock:
            proc = self._processes.get(project_id)
            if proc is None:
                return "stopped"

            poll = proc.poll()
            if poll is None:
                project = self._find_project(project_id)
                port = project["port"] if project else 0
                if port and is_port_open(port):
                    return "running"
                return "starting"
            else:
                return "error" if poll != 0 else "stopped"

    def get_pid(self, project_id):
        """获取进程 PID"""
        with self._lock:
            proc = self._processes.get(project_id)
            if proc is not None and proc.poll() is None:
                return proc.pid
            return None

    def start(self, project_id):
        """启动进程"""
        project = self._find_project(project_id)
        if not project:
            return False, f"项目 {project_id} 不存在"

        with self._lock:
            if self._processes.get(project_id) is not None:
                if self._processes[project_id].poll() is None:
                    return False, f"{project['name']} 已在运行中"
                self._processes[project_id] = None

        path = project["path"].strip()
        framework = get_framework(project.get("framework"))
        command, cmd_src = resolve_command(project, framework, "command", "startCommand")
        pre_command = project.get("preCommand", "").strip()

        if not path:
            return False, f"请先配置 [{project['name']}] 项目路径"
        if not os.path.isdir(path):
            return False, f"[{project['name']}] 项目路径不存在: {path}"
        if not command:
            return False, f"请先配置 [{project['name']}] 启动命令（或在框架中设置默认启动命令）"

        log_file = self._log_path(project_id)
        # 清空旧日志
        with open(log_file, "wb") as f:
            lines = [
                f"=== {project['name']} ({project_id}) 日志 ===",
                f"=== 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===",
                f"=== 目录: {path} ===",
            ]
            if pre_command:
                lines.append(f"=== 前置命令: {pre_command} ===")
            lines.append(f"=== 命令: {command} ===")
            lines.append("")
            f.write("\n".join(lines).encode(_SYS_ENCODING))

        try:
            # 子进程输出写入日志（二进制模式避免编码转换），系统编码用于 read_log 解码
            log_fh = open(log_file, "ab")
            if sys.platform == "win32":
                # Windows: 写成临时 .bat 文件执行，确保 nvm use 等命令的
                # 环境变量能正确传递给后续命令（& 拼接做不到）
                # 注意：nvm 也是批处理，需用 call 调用，否则会终止父批处理
                bat_fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="launcher_")
                bat_lines = ['@echo off', f'cd /d "{path}"']
                # 注入指定 Node 版本到 PATH（进程级隔离，不影响全局）
                node_version = project.get("nodeVersion", "").strip()
                if node_version:
                    nvm_home = os.environ.get("NVM_HOME", r"D:\nvm")
                    bat_lines.append(f'set "PATH={nvm_home}\\v{node_version};%PATH%"')
                if pre_command:
                    bat_lines.append(f"call {pre_command}")
                bat_lines.append(command)
                bat_content = "\r\n".join(bat_lines) + "\r\n"
                with os.fdopen(bat_fd, "w", encoding=_SYS_ENCODING) as f:
                    f.write(bat_content)
                proc = subprocess.Popen(
                    bat_path,
                    shell=True,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                # 等进程结束后清理临时文件
                def _cleanup():
                    try:
                        proc.wait()
                    except Exception:
                        pass
                    try:
                        os.remove(bat_path)
                    except OSError:
                        pass
                threading.Thread(target=_cleanup, daemon=True).start()
            else:
                proc = subprocess.Popen(
                    command,
                    cwd=path,
                    shell=True,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )

            with self._lock:
                self._processes[project_id] = proc

            # 关闭我们的文件句柄，子进程持有独立的副本
            log_fh.close()

            return True, f"{project['name']} 启动成功 (PID: {proc.pid})"

        except Exception as e:
            try:
                log_fh.close()
                with open(log_file, "ab") as f:
                    f.write(f"\n启动失败: {e}\n".encode(_SYS_ENCODING))
            except Exception:
                pass
            return False, f"{project['name']} 启动失败: {e}"

    def stop(self, project_id):
        """停止进程"""
        with self._lock:
            proc = self._processes.get(project_id)
            if proc is None or proc.poll() is not None:
                self._processes[project_id] = None
                return True, None

            pid = proc.pid
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=15,
                    )
                else:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                    time.sleep(1)
                    if proc.poll() is None:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                pass

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

            self._processes[project_id] = None

            project = self._find_project(project_id)
            name = project["name"] if project else project_id
            return True, f"{name} 已停止"

    def read_log(self, project_id, lines=200):
        """读取最近的日志行（自动处理 GBK/UTF-8 混合编码）"""
        log_file = self._log_path(project_id)
        if not log_file.exists():
            return ""
        try:
            with open(log_file, "rb") as f:
                raw = f.read()
            # 优先系统编码（GBK），失败则 UTF-8
            for enc in [_SYS_ENCODING, "gbk", "gb2312", "utf-8", "latin-1"]:
                try:
                    content = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                content = raw.decode("utf-8", errors="replace")
            all_lines = content.split("\n")
            return "\n".join(all_lines[-lines:])
        except Exception:
            return ""


# 全局进程管理器
pm = ProcessManager()


# ============================================================
# VCS 操作（拉取 / 提交 / 打包）—— 移植自 project_builder
# 与「编译」一致：后台线程执行，日志写入项目自身的 .log 文件
# ============================================================
_vcs_projects = {}          # project_id -> 当前操作名（拉取/提交/打包）
_vcs_lock = threading.Lock()


def detect_vcs(project_path):
    """检测路径自身的版本控制类型"""
    if os.path.isdir(os.path.join(project_path, '.svn')):
        return 'svn'
    if os.path.isdir(os.path.join(project_path, '.git')):
        return 'git'
    return 'none'


def detect_vcs_for_path(project_path):
    """向上查找父目录，确定项目使用的版本控制"""
    current = os.path.abspath(project_path)
    while True:
        vcs = detect_vcs(current)
        if vcs != 'none':
            return vcs
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return 'none'


def find_node_build_output(project_path):
    """查找 Node 项目的构建产物目录"""
    for d in ['dist', 'build', 'out']:
        p = os.path.join(project_path, d)
        if os.path.isdir(p):
            return p
    return None


def find_maven_artifacts(project_path, artifact_pattern=None):
    """查找 Maven 项目的 jar/war 产物"""
    artifacts = []
    if artifact_pattern:
        patterns = [p.strip() for p in artifact_pattern.split(',') if p.strip()]
        for pat in patterns:
            matched = glob.glob(os.path.join(project_path, pat))
            for m in matched:
                if os.path.isfile(m) and (m.endswith('.jar') or m.endswith('.war')):
                    artifacts.append(m)
    else:
        skip_dirs = {'node_modules', '.git', '.svn', 'lib', 'libs', 'src'}
        skip_prefixes = ('original-', 'sources', 'javadoc', 'tests')
        skip_suffixes = ('-sources.jar', '-javadoc.jar', '.tests.jar',
                         '-tests.jar', '-test.jar')
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            if os.path.basename(root) != 'target':
                continue
            for f in files:
                if not (f.endswith('.jar') or f.endswith('.war')):
                    continue
                if any(f.endswith(s) for s in skip_suffixes):
                    continue
                if any(f.startswith(p) for p in skip_prefixes):
                    continue
                if '-sources' in f or '-javadoc' in f:
                    continue
                artifacts.append(os.path.join(root, f))
    return artifacts


def _vcs_exec(write, cmd, cwd):
    """执行命令并逐行写日志，返回 returncode。cmd 可为 list（shell=False）或 str（shell=True）"""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, shell=isinstance(cmd, str),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for raw in proc.stdout:
            try:
                line = raw.decode(_SYS_ENCODING, errors='replace').rstrip()
            except Exception:
                line = raw.decode('utf-8', errors='replace').rstrip()
            if line:
                write('   ' + line)
        proc.wait()
        return proc.returncode
    except FileNotFoundError:
        write('   ✗ 命令未找到: ' + (cmd if isinstance(cmd, str) else cmd[0]))
        return -1


def _copy_artifacts(write, project, path, dest_root, artifact_path=None):
    """将构建产物复制到 dest_root/<项目名>"""
    name = project.get('name', '')
    dest = os.path.join(dest_root, name)
    if artifact_path is None:
        artifact_path = project.get('artifactPath', '').strip()

    if artifact_path:
        src = artifact_path if os.path.isabs(artifact_path) else os.path.join(path, artifact_path)
        if os.path.isdir(src):
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            write(f'   ✓ {artifact_path}/ -> {dest}')
        elif os.path.isfile(src):
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(src, dest)
            write(f'   ✓ {artifact_path} -> {dest}')
        else:
            matched = glob.glob(src)
            files = [m for m in matched if os.path.isfile(m)]
            if files:
                os.makedirs(dest, exist_ok=True)
                for f in files:
                    shutil.copy2(f, dest)
                write(f'   ✓ {len(files)} 个文件 -> {dest}')
            else:
                write(f'   ⚠ 产物路径不存在: {src}')
        return

    ptype = 'maven' if os.path.exists(os.path.join(path, 'pom.xml')) \
        else ('node' if os.path.exists(os.path.join(path, 'package.json')) else 'unknown')
    if ptype == 'maven':
        artifacts = find_maven_artifacts(path)
        if artifacts:
            os.makedirs(dest, exist_ok=True)
            for a in artifacts:
                shutil.copy2(a, dest)
                write(f'   ✓ {os.path.relpath(a, path)} -> {dest}')
        else:
            write('   ⚠ 未找到 jar/war 产物')
    elif ptype == 'node':
        src = find_node_build_output(path)
        if src:
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            write(f'   ✓ {os.path.basename(src)}/ -> {dest}')
        else:
            write('   ⚠ 未找到 dist/build/out 目录')
    else:
        write('   ⚠ 无法识别项目类型，跳过产物复制')


def _run_vcs(project_id, operation, message=None, log_file=None):
    """后台线程：执行 pull / commit / package 操作"""
    try:
        project = pm._find_project(project_id)
        if not project:
            return
        path = project.get('path', '').strip()
        name = project.get('name', project_id)
        config = load_config()

        def write(s):
            try:
                with open(log_file, 'ab') as fh:
                    fh.write(('\n' + s).encode(_SYS_ENCODING, errors='replace'))
                    fh.flush()
            except Exception:
                pass

        if not path or not os.path.isdir(path):
            write(f'✗ [{name}] 项目路径无效: {path}')
            return

        vcs = detect_vcs_for_path(path)
        if vcs == 'none':
            write(f'⚠ [{name}] 未检测到 .svn 或 .git，无法执行{operation}')
            return

        if operation == 'pull':
            write(f'▶ 拉取代码: {name}  [{vcs}]')
            cmd = ['svn', 'update'] if vcs == 'svn' else ['git', 'pull']
            rc = _vcs_exec(write, cmd, path)
            write('   ✓ 拉取完成' if rc == 0 else f'   ⚠ 拉取返回码 {rc}')

        elif operation == 'commit':
            msg = (message or '').strip() or datetime.now().strftime('提交于 %Y-%m-%d %H:%M:%S')
            write(f'▶ 提交代码: {name}  [{vcs}]')
            write(f'   提交信息: {msg}')
            if vcs == 'svn':
                rc = _vcs_exec(write, ['svn', 'commit', '-m', msg], path)
                write('   ✓ 提交成功' if rc == 0 else f'   ✗ 提交失败（返回码 {rc}）')
            else:
                _vcs_exec(write, ['git', 'add', '-A'], path)
                rc = _vcs_exec(write, ['git', 'commit', '-m', msg], path)
                if rc == 0:
                    write('   ✓ 本地提交成功')
                else:
                    write('   ⚠ git commit 返回码 %s（可能没有变更）' % rc)
                rc = _vcs_exec(write, ['git', 'push'], path)
                write('   ✓ 推送成功' if rc == 0 else f'   ✗ 推送失败（返回码 {rc}）')

        elif operation == 'package':
            framework = get_framework(project.get('framework'))
            package_cmd, pkg_src = resolve_command(project, framework, 'packageCommand', 'packageCommand')
            if not package_cmd:
                package_cmd = project.get('compileCommand', '').strip() or 'mvn clean install -DskipTests'
            artifact_path = resolve_artifact_path(project, framework)
            if config.get('package_auto_pull', True):
                write(f'▶ 拉取最新代码: {name}  [{vcs}]')
                cmd = ['svn', 'update'] if vcs == 'svn' else ['git', 'pull']
                rc = _vcs_exec(write, cmd, path)
                if rc != 0:
                    write(f'   ⚠ 拉取返回码 {rc}，继续打包')
            write(f'▶ 开始打包: {name}')
            write(f'   ▷ {package_cmd}' + (f'  (来源: {pkg_src})' if pkg_src else ''))
            rc = _vcs_exec(write, package_cmd, path)
            if rc != 0:
                write(f'   ✗ 打包失败（返回码 {rc}）')
                return
            write('   ✓ 构建成功')
            dest_root = (project.get('packageTargetDir', '') or config.get('package_target_dir', '') or config.get('package_output_dir', '')).strip()
            if dest_root:
                if not os.path.isdir(dest_root):
                    try:
                        os.makedirs(dest_root, exist_ok=True)
                        write(f'   ✓ 已创建目录: {dest_root}')
                    except Exception as e:
                        write(f'   ⚠ 目录不存在且无法创建: {dest_root}（{e}）')
                        dest_root = ''
                if dest_root:
                    write(f'▶ 复制产物到: {dest_root}')
                    _copy_artifacts(write, project, path, dest_root, artifact_path)
            else:
                write('   ⚠ 未配置打包输出目录（在项目设置填写「打包输出目录」，或配置全局 package_target_dir），跳过产物复制')
            write('✓ 打包完成!')
    finally:
        with _vcs_lock:
            _vcs_projects.pop(project_id, None)


def _start_vcs_op(project_id, operation, label=None):
    """校验并启动一个 VCS 后台操作，返回 (ok, message)。
    operation 为内部键 pull/commit/package；label 为界面显示名（拉取/提交/打包）"""
    label = label or operation
    project = pm._find_project(project_id)
    if not project:
        return False, f'项目 {project_id} 不存在'
    path = project.get('path', '').strip()
    if not path or not os.path.isdir(path):
        return False, f"[{project['name']}] 项目路径无效: {path}"
    with _vcs_lock:
        if project_id in _vcs_projects:
            return False, f"{project['name']} 正在执行「{_vcs_projects[project_id]}」操作..."
        _vcs_projects[project_id] = label
    log_file = pm._log_path(project_id)
    t = threading.Thread(
        target=_run_vcs,
        kwargs={'project_id': project_id, 'operation': operation, 'log_file': log_file},
        daemon=True,
    )
    t.start()
    return True, f"{project['name']} 开始{label}..."


# ============================================================
# 远程管理器 (SSH)
# ============================================================
class RemoteManager:
    """通过 SSH 管理远程 Linux 服务器上的项目"""

    def __init__(self):
        self._lock = threading.Lock()
        self._remote_pids = {}   # {project_id: remote_pid}
        self._compiling_remote = set()
        self._compiling_lock = threading.Lock()

    def _get_server(self, server_id):
        """从配置中查找服务器"""
        config = load_config()
        for s in config.get("remoteServers", []):
            if s["id"] == server_id:
                return s
        return None

    def _find_project(self, project_id):
        """从配置中查找远程项目"""
        config = load_config()
        for p in config.get("remoteProjects", []):
            if p["id"] == project_id:
                return p
        return None

    def _get_ssh(self, server):
        """创建 SSH 连接"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=server["host"],
            port=int(server.get("port", 22)),
            username=server["username"],
            password=server["password"],
            timeout=10,
        )
        return client

    def exec_command(self, server, command, cwd=None):
        """SSH 执行远程命令，返回 (stdout, stderr, returncode)"""
        client = None
        try:
            client = self._get_ssh(server)
            if cwd:
                command = f"cd {cwd} && {command}"
            stdin, stdout, stderr = client.exec_command(command, timeout=30)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            rc = stdout.channel.recv_exit_status()
            return out, err, rc
        except Exception as e:
            return "", str(e), -1
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def test_connection(self, server):
        """测试 SSH 连接"""
        client = None
        try:
            client = self._get_ssh(server)
            stdin, stdout, stderr = client.exec_command("echo ok", timeout=5)
            out = stdout.read().decode().strip()
            return out == "ok", f"连接成功: {server['host']}"
        except Exception as e:
            return False, f"连接失败: {e}"
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def _detect_node_path(self, command):
        """从命令中检测 Node.js 路径，用于设置 PATH 环境变量"""
        if "/bin/npm" in command or "/bin/node" in command:
            match = re.search(r'(/[^\s]+/bin)/(?:npm|node)', command)
            if match:
                return match.group(1)
        return ""

    def start_project(self, server, project):
        """远程启动项目"""
        path = project["path"].strip()
        framework = get_framework(project.get("framework"))
        command, cmd_src = resolve_command(project, framework, "command", "startCommand")
        # 自动转换 Windows 风格路径分隔符为 Linux 风格
        command = command.replace('\\', '/')
        if not path:
            return False, f"请先配置 [{project['name']}] 项目路径"
        if not command:
            return False, f"请先配置 [{project['name']}] 启动命令"

        log_path = project.get("logPath", f"{path}/launcher.log").strip()
        # 写入日志头
        header_cmd = (
            f'echo "=== {project["name"]} ({project["id"]}) 日志 ===" > {log_path} && '
            f'echo "=== 启动时间: $(date \'+%Y-%m-%d %H:%M:%S\') ===" >> {log_path} && '
            f'echo "=== 目录: {path} ===" >> {log_path} && '
            f'echo "=== 命令: {command} ===" >> {log_path}'
        )
        self.exec_command(server, header_cmd)

        # 检测 Node.js 路径并设置 PATH
        node_path = self._detect_node_path(command)

        # nohup 后台启动
        if node_path:
            start_cmd = f"export PATH={node_path}:$PATH && nohup {command} >> {log_path} 2>&1 & echo $!"
        else:
            start_cmd = f"nohup {command} >> {log_path} 2>&1 & echo $!"
        out, err, rc = self.exec_command(server, start_cmd, cwd=path)

        if rc != 0 and not out.strip():
            return False, f"启动失败: {err or '未知错误'}"

        remote_pid = out.strip().split("\n")[-1].strip()
        with self._lock:
            self._remote_pids[project["id"]] = remote_pid

        return True, f"{project['name']} 已在远程服务器启动 (PID: {remote_pid})"

    def stop_project(self, server, project):
        """远程停止项目"""
        port = project.get("port", 0)
        if port:
            # 按端口杀进程
            cmd = f"lsof -ti:{port} 2>/dev/null | xargs -r kill -15 2>/dev/null; sleep 1; lsof -ti:{port} 2>/dev/null | xargs -r kill -9 2>/dev/null; echo done"
            out, err, rc = self.exec_command(server, cmd)
            if "done" in out:
                with self._lock:
                    self._remote_pids.pop(project["id"], None)
                return True, f"{project['name']} 已在远程服务器停止"
            # 回退: fuser
            cmd2 = f"fuser -k {port}/tcp 2>/dev/null; echo done"
            out2, _, _ = self.exec_command(server, cmd2)
            if "done" in out2:
                with self._lock:
                    self._remote_pids.pop(project["id"], None)
                return True, f"{project['name']} 已在远程服务器停止"
            return False, f"停止失败: {err or '无法找到进程'}"
        else:
            # 无端口，用 PID 杀
            with self._lock:
                pid = self._remote_pids.get(project["id"])
            if pid:
                cmd = f"kill -15 {pid} 2>/dev/null; sleep 1; kill -9 {pid} 2>/dev/null; echo done"
                out, _, _ = self.exec_command(server, cmd)
                with self._lock:
                    self._remote_pids.pop(project["id"], None)
                return True, f"{project['name']} 已在远程服务器停止"
            return False, "无法确定进程 PID（未配置端口且无记录 PID）"

    def get_status(self, server, project):
        """远程判断运行状态"""
        port = project.get("port", 0)
        if port:
            cmd = f"ss -tlnp 2>/dev/null | grep ':{port} ' || netstat -tlnp 2>/dev/null | grep ':{port} '"
            out, _, rc = self.exec_command(server, cmd)
            if out.strip():
                return "running"
            return "stopped"
        else:
            # 无端口，检查记录的 PID
            with self._lock:
                pid = self._remote_pids.get(project["id"])
            if pid:
                cmd = f"ps -p {pid} -o pid= 2>/dev/null"
                out, _, _ = self.exec_command(server, cmd)
                if out.strip():
                    return "running"
            return "stopped"

    def read_log(self, server, project, lines=400):
        """SSH 读取远程日志"""
        path = project["path"].strip()
        log_path = project.get("logPath", f"{path}/launcher.log").strip()
        cmd = f"tail -n {lines} {log_path} 2>/dev/null"
        out, err, rc = self.exec_command(server, cmd)
        if rc != 0 and not out:
            return f"读取日志失败: {err}"
        return out

    def compile_project(self, server, project):
        """远程编译（后台线程）"""
        project_id = project["id"]
        with self._compiling_lock:
            if project_id in self._compiling_remote:
                return False, f"{project['name']} 正在编译中..."
            self._compiling_remote.add(project_id)

        path = project["path"].strip()
        framework = get_framework(project.get("framework"))
        compile_cmd, cmd_src = resolve_command(project, framework, "compileCommand", "compileCommand")
        # 自动转换 Windows 风格路径分隔符为 Linux 风格
        compile_cmd = compile_cmd.replace('\\', '/')
        if not compile_cmd:
            compile_cmd = "mvn clean install -DskipTests"
        log_path = project.get("logPath", f"{path}/launcher.log").strip()

        # 检测 Node.js 路径并设置 PATH
        node_path = self._detect_node_path(compile_cmd)

        def _run():
            try:
                if node_path:
                    cmd = f"cd {path} && export PATH={node_path}:$PATH && {compile_cmd} >> {log_path} 2>&1; echo '=== 编译完成，退出码: $? ===' >> {log_path}"
                else:
                    cmd = f"cd {path} && {compile_cmd} >> {log_path} 2>&1; echo '=== 编译完成，退出码: $? ===' >> {log_path}"
                self.exec_command(server, cmd)
            except Exception as e:
                try:
                    header_cmd = f'echo "=== 编译失败: {e} ===" >> {log_path}'
                    self.exec_command(server, header_cmd)
                except Exception:
                    pass
            finally:
                with self._compiling_lock:
                    self._compiling_remote.discard(project_id)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return True, f"{project['name']} 开始远程编译..."

    def is_compiling(self, project_id):
        with self._compiling_lock:
            return project_id in self._compiling_remote

    def sftp_download(self, server, remote_project_path, artifact_path, local_dest):
        """把远程匹配 artifact_path 的文件下载到 local_dest（支持通配符文件名）。
        artifact_path 为相对项目目录的路径，如 target/*.jar 或 yudao-server/target/*.jar"""
        import fnmatch, stat as _stat
        ssh = self._get_ssh(server)
        try:
            sftp = ssh.open_sftp()
            norm = (artifact_path or "").replace('\\', '/').strip()
            if not norm:
                norm = "target/*.jar"
            if '/' in norm:
                rdir = os.path.dirname(norm)
                pattern = os.path.basename(norm)
                remote_dir = remote_project_path.rstrip('/') + '/' + rdir
            else:
                remote_dir = remote_project_path
                pattern = norm
            try:
                entries = sftp.listdir_attr(remote_dir)
            except IOError:
                entries = []
            files = [e for e in entries
                     if not _stat.S_ISDIR(e.st_mode) and fnmatch.fnmatch(e.filename, pattern)]
            os.makedirs(local_dest, exist_ok=True)
            if not files:
                # 把整个 artifact_path 当作已知相对文件再试一次
                try:
                    rfile = remote_project_path.rstrip('/') + '/' + norm
                    lfile = os.path.join(local_dest, os.path.basename(norm))
                    sftp.get(rfile, lfile)
                    return [lfile]
                except IOError:
                    return []
            downloaded = []
            for e in files:
                rfile = remote_dir.rstrip('/') + '/' + e.filename
                lfile = os.path.join(local_dest, e.filename)
                sftp.get(rfile, lfile)
                downloaded.append(lfile)
            return downloaded
        finally:
            try:
                ssh.close()
            except Exception:
                pass

    def package_project(self, server, project):
        """远程打包：先执行打包命令构建产物，再把产物下载到本地产物目录"""
        project_id = project["id"]
        with self._compiling_lock:
            if project_id in self._compiling_remote:
                return False, f"{project['name']} 正在编译/打包中..."
            self._compiling_remote.add(project_id)

        path = project["path"].strip()
        framework = get_framework(project.get("framework"))
        package_cmd, pkg_src = resolve_command(project, framework, "packageCommand", "packageCommand")
        if not package_cmd:
            package_cmd = project.get("compileCommand", "").strip() or "mvn clean install -DskipTests"
        package_cmd = package_cmd.replace('\\', '/')
        artifact_path = resolve_artifact_path(project, framework)
        log_path = project.get("logPath", f"{path}/launcher.log").strip()
        node_path = self._detect_node_path(package_cmd)

        def _run():
            try:
                if node_path:
                    build_line = (f"cd {path} && export PATH={node_path}:$PATH && {package_cmd} "
                                  f">> {log_path} 2>&1; echo '=== 打包完成，退出码: $? ===' >> {log_path}")
                else:
                    build_line = (f"cd {path} && {package_cmd} "
                                  f">> {log_path} 2>&1; echo '=== 打包完成，退出码: $? ===' >> {log_path}")
                self.exec_command(server, build_line)
                cfg = load_config()
                dest_root = (project.get("packageTargetDir", "") or cfg.get("package_target_dir", "")
                             or cfg.get("package_output_dir", "")).strip()
                if dest_root:
                    os.makedirs(dest_root, exist_ok=True)
                    local_dest = os.path.join(dest_root, project.get("name", project_id))
                    try:
                        files = self.sftp_download(server, path, artifact_path, local_dest)
                        with open(os.path.join(dest_root, ".remote_package.log"), "a", encoding="utf-8") as lf:
                            lf.write(f"{project.get('name')} 已下载 {len(files)} 个产物到 {local_dest}\n")
                    except Exception as e:
                        with open(os.path.join(dest_root, ".remote_package_err.log"), "a", encoding="utf-8") as ef:
                            ef.write(f"{project.get('name')} 产物复制失败: {e}\n")
            except Exception as e:
                try:
                    self.exec_command(server, f'echo "=== 打包失败: {e} ===" >> {log_path}')
                except Exception:
                    pass
            finally:
                with self._compiling_lock:
                    self._compiling_remote.discard(project_id)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return True, f"{project['name']} 开始远程打包..."


# 全局远程管理器
rm = RemoteManager()


# ============================================================
# 配置管理
# ============================================================
def load_config():
    """加载配置：优先读取 config.json，不存在则创建空配置（不预置任何项目）"""
    if CONFIG_FILE.exists() and CONFIG_FILE.stat().st_size > 0:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = migrate_old_config(data)
            if "projects" not in data:
                data["projects"] = []
            if "remoteServers" not in data:
                data["remoteServers"] = []
            if "remoteProjects" not in data:
                data["remoteProjects"] = []
            if "server" not in data:
                data["server"] = {"port": 5150}
            if "frameworks" not in data or not data.get("frameworks"):
                data["frameworks"] = [dict(fw) for fw in DEFAULT_FRAMEWORKS]
            return data
        except (json.JSONDecodeError, Exception) as e:
            # 配置损坏，备份后重建
            backup = CONFIG_FILE.with_suffix(".json.bak")
            try:
                import shutil
                shutil.copy2(CONFIG_FILE, backup)
                print(f"[WARN] 配置文件损坏，已备份为 {backup}，使用空配置重新开始")
            except Exception:
                print(f"[WARN] 配置文件读取失败: {e}，使用空配置重新开始")
    # 文件不存在或为空 → 返回全新空配置
    return {"projects": [], "remoteServers": [], "remoteProjects": [], "frameworks": [dict(fw) for fw in DEFAULT_FRAMEWORKS], "server": {"port": 5150}}


def save_config(config):
    """保存配置：原子写入（先写临时文件再替换），防止写入中断导致损坏"""
    config = migrate_old_config(config)
    tmp_file = CONFIG_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        tmp_file.replace(CONFIG_FILE)  # 原子替换（Windows 要求 CONFIG_FILE 已存在）
    except Exception:
        # 回退到直接写入
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)


# ============================================================
# 工具函数
# ============================================================
def is_port_open(port, host="127.0.0.1", timeout=1):
    """检查端口是否开放"""
    import socket
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


# ============================================================
# Flask 应用
# ============================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = "project-launcher-2024"


@app.route("/")
def index():
    return render_template("index.html")


# === 状态 API ===
@app.route("/api/status")
def api_status():
    """获取所有项目状态"""
    config = load_config()
    result = {}
    for p in config["projects"]:
        pid_val = pm.get_pid(p["id"])
        result[p["id"]] = {
            "status": pm.get_status(p["id"]),
            "pid": pid_val,
            "port": p["port"],
            "name": p["name"],
        }
    return jsonify(result)


# === 项目 CRUD API ===
@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    """列出所有项目"""
    config = load_config()
    return jsonify(config["projects"])


@app.route("/api/projects", methods=["POST"])
def api_create_project():
    """新增项目"""
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"ok": False, "message": "项目名称不能为空"}), 400

    config = load_config()

    # 生成/验证 ID
    pid = data.get("id", "").strip()
    if not pid:
        pid = _safe_id(data["name"])
    else:
        pid = _safe_id(pid)

    # 检查 ID 唯一性
    for p in config["projects"]:
        if p["id"] == pid:
            return jsonify({"ok": False, "message": f"项目 ID '{pid}' 已存在"}), 400

    # 自动分配 startOrder
    max_order = max((p.get("startOrder", 0) for p in config["projects"]), default=0)

    project = {
        "id": pid,
        "name": data["name"].strip(),
        "path": data.get("path", "").strip(),
        "preCommand": data.get("preCommand", "").strip(),
        "command": data.get("command", "").strip(),
        "compileCommand": data.get("compileCommand", "").strip(),
        "packageCommand": data.get("packageCommand", "").strip(),
        "framework": data.get("framework", "").strip(),
        "nodeVersion": data.get("nodeVersion", "").strip(),
        "artifactPath": data.get("artifactPath", "").strip(),
        "packageTargetDir": data.get("packageTargetDir", "").strip(),
        "port": int(data.get("port", 0)),
        "enabled": data.get("enabled", True),
        "startOrder": data.get("startOrder", max_order + 1),
    }
    config["projects"].append(project)
    save_config(config)
    return jsonify({"ok": True, "message": f"项目 '{project['name']}' 已添加", "project": project})


@app.route("/api/projects/<project_id>", methods=["PUT"])
def api_update_project(project_id):
    """编辑项目"""
    data = request.get_json()
    config = load_config()

    for i, p in enumerate(config["projects"]):
        if p["id"] == project_id:
            if "name" in data:
                p["name"] = data["name"].strip()
            if "path" in data:
                p["path"] = data["path"].strip()
            if "command" in data:
                p["command"] = data["command"].strip()
            if "preCommand" in data:
                p["preCommand"] = data["preCommand"].strip()
            if "compileCommand" in data:
                p["compileCommand"] = data["compileCommand"].strip()
            if "packageCommand" in data:
                p["packageCommand"] = data["packageCommand"].strip()
            if "framework" in data:
                p["framework"] = data["framework"].strip()
            if "nodeVersion" in data:
                p["nodeVersion"] = data["nodeVersion"].strip()
            if "artifactPath" in data:
                p["artifactPath"] = data["artifactPath"].strip()
            if "packageTargetDir" in data:
                p["packageTargetDir"] = data["packageTargetDir"].strip()
            if "port" in data:
                p["port"] = int(data["port"])
            if "enabled" in data:
                p["enabled"] = bool(data["enabled"])
            if "startOrder" in data:
                p["startOrder"] = int(data["startOrder"])
            if "id" in data and data["id"] != project_id:
                new_id = _safe_id(data["id"])
                # 检查新 ID 是否冲突
                if any(x["id"] == new_id for x in config["projects"]):
                    return jsonify({"ok": False, "message": f"项目 ID '{new_id}' 已存在"}), 400
                p["id"] = new_id
            config["projects"][i] = p
            save_config(config)
            return jsonify({"ok": True, "message": f"项目 '{p['name']}' 已更新", "project": p})

    return jsonify({"ok": False, "message": "项目不存在"}), 404


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def api_delete_project(project_id):
    """删除项目（先停止再删除）"""
    # 先停止
    pm.stop(project_id)

    config = load_config()
    config["projects"] = [p for p in config["projects"] if p["id"] != project_id]
    save_config(config)

    # 清理日志（可选）
    log_file = LOGS_DIR / f"{project_id}.log"
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass

    return jsonify({"ok": True, "message": "项目已删除"})


# === 操作 API ===
@app.route("/api/projects/<project_id>/start", methods=["POST"])
def api_start(project_id):
    """启动单个项目"""
    ok, msg = pm.start(project_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/projects/<project_id>/stop", methods=["POST"])
def api_stop(project_id):
    """停止单个项目"""
    ok, msg = pm.stop(project_id)
    return jsonify({"ok": ok, "message": msg or "已停止"})


@app.route("/api/start-all", methods=["POST"])
def api_start_all():
    """一键启动：按 startOrder 逐个启动，等待就绪后启动下一个"""
    config = load_config()
    projects = sorted(
        [p for p in config["projects"] if p.get("enabled", True)],
        key=lambda p: p.get("startOrder", 999),
    )

    if not projects:
        return jsonify({"ok": False, "message": "没有可启动的项目"})

    started = []
    for project in projects:
        pid = project["id"]
        name = project["name"]

        # 跳过已运行的项目
        if pm.get_status(pid) == "running":
            started.append(name)
            continue

        ok, msg = pm.start(pid)
        if not ok:
            return jsonify({
                "ok": False,
                "message": f"{name} 启动失败: {msg}",
                "started": started,
            })

        # 等待当前项目就绪
        port = project.get("port", 0)
        if port:
            ready = False
            for _ in range(180):  # 最多等 3 分钟
                time.sleep(1)
                status = pm.get_status(pid)
                if status == "running":
                    ready = True
                    break
                if status == "error":
                    return jsonify({
                        "ok": False,
                        "message": f"{name} 启动后异常退出",
                        "started": started,
                    })
            if not ready:
                return jsonify({
                    "ok": False,
                    "message": f"{name} 启动超时（3分钟），请检查日志",
                    "started": started,
                })
        else:
            # 无端口配置，简单等待 3 秒
            time.sleep(3)

        started.append(name)

    return jsonify({
        "ok": True,
        "message": f"已启动: {', '.join(started)}" if started else "所有项目已在运行中",
        "started": started,
    })


@app.route("/api/stop-all", methods=["POST"])
def api_stop_all():
    """一键停止：按 startOrder 反序停止"""
    config = load_config()
    projects = sorted(config["projects"], key=lambda p: p.get("startOrder", 999), reverse=True)

    results = {}
    for project in projects:
        pid = project["id"]
        ok, msg = pm.stop(pid)
        results[pid] = {"ok": ok, "message": msg or "已停止"}

    return jsonify(results)


# === 编译 API ===
# 跟踪正在编译的项目
_compiling_projects = set()
_compiling_lock = threading.Lock()


def _run_compile(project_id, project, path, compile_cmd, log_file):
    """后台线程执行编译"""
    try:
        with open(log_file, "ab") as log_fh:
            log_fh.write(f"\n=== 执行编译: {compile_cmd} ===\n".encode(_SYS_ENCODING))
            log_fh.flush()
            proc = subprocess.run(
                compile_cmd,
                cwd=path,
                shell=True,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=600,
            )
            result_msg = f"编译完成，返回码: {proc.returncode}"
            log_fh.write(f"=== {result_msg} ===\n".encode(_SYS_ENCODING))
    except subprocess.TimeoutExpired:
        try:
            with open(log_file, "ab") as log_fh:
                log_fh.write(f"=== 编译超时（10分钟） ===\n".encode(_SYS_ENCODING))
        except Exception:
            pass
    except Exception as e:
        try:
            with open(log_file, "ab") as log_fh:
                log_fh.write(f"=== 编译失败: {e} ===\n".encode(_SYS_ENCODING))
        except Exception:
            pass
    finally:
        with _compiling_lock:
            _compiling_projects.discard(project_id)


@app.route("/api/projects/<project_id>/compile", methods=["POST"])
def api_compile(project_id):
    """对项目执行编译（后台异步执行）"""
    project = pm._find_project(project_id)
    if not project:
        return jsonify({"ok": False, "message": f"项目 {project_id} 不存在"})

    path = project["path"].strip()
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "message": f"[{project['name']}] 项目路径无效: {path}"})

    # 检查是否正在编译
    with _compiling_lock:
        if project_id in _compiling_projects:
            return jsonify({"ok": False, "message": f"{project['name']} 正在编译中..."})
        _compiling_projects.add(project_id)

    framework = get_framework(project.get("framework"))
    compile_cmd, cmd_src = resolve_command(project, framework, "compileCommand", "compileCommand")
    if not compile_cmd:
        compile_cmd = "mvn clean install -DskipTests"
    log_file = pm._log_path(project_id)

    # 后台线程执行编译
    t = threading.Thread(
        target=_run_compile,
        args=(project_id, project, path, compile_cmd, log_file),
        daemon=True,
    )
    t.start()

    return jsonify({"ok": True, "message": f"{project['name']} 开始编译..."})


@app.route("/api/projects/<project_id>/compiling")
def api_compiling(project_id):
    """检查项目是否正在编译"""
    with _compiling_lock:
        is_compiling = project_id in _compiling_projects
    return jsonify({"compiling": is_compiling})


# === VCS 操作 API（拉取 / 提交 / 打包） ===
@app.route("/api/projects/<project_id>/pull", methods=["POST"])
def api_pull(project_id):
    """拉取代码（git pull / svn update）"""
    ok, msg = _start_vcs_op(project_id, "pull", "拉取")
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/projects/<project_id>/commit", methods=["POST"])
def api_commit(project_id):
    """提交代码（git add/commit/push 或 svn commit）"""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    # 先把 message 传给启动检查（路径校验），再启动后台线程
    project = pm._find_project(project_id)
    if not project:
        return jsonify({"ok": False, "message": f"项目 {project_id} 不存在"}), 404
    path = project.get("path", "").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "message": f"[{project['name']}] 项目路径无效: {path}"})
    with _vcs_lock:
        if project_id in _vcs_projects:
            return jsonify({"ok": False, "message": f"{project['name']} 正在执行「{_vcs_projects[project_id]}」操作..."})
        _vcs_projects[project_id] = "提交"
    log_file = pm._log_path(project_id)
    t = threading.Thread(
        target=_run_vcs,
        kwargs={"project_id": project_id, "operation": "commit", "message": message, "log_file": log_file},
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "message": f"{project['name']} 开始提交..."})


@app.route("/api/projects/<project_id>/package", methods=["POST"])
def api_package(project_id):
    """打包（拉取最新 + 执行编译命令 + 复制产物）"""
    ok, msg = _start_vcs_op(project_id, "package", "打包")
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/projects/<project_id>/vcs-status")
def api_vcs_status(project_id):
    """查询项目是否正在执行 VCS 操作"""
    with _vcs_lock:
        op = _vcs_projects.get(project_id)
    return jsonify({"busy": op is not None, "operation": op or ""})


# === 文件夹选择 API ===
@app.route("/api/browse-folder", methods=["POST"])
def api_browse_folder():
    """弹出系统文件夹选择框，返回选中的绝对路径。

    仅在本机交互式会话下可用（由浏览器调用本机运行的 Flask 服务时）。
    initial 为初始目录；用户取消则返回 path=null。
    """
    initial = ""
    try:
        data = request.get_json(silent=True) or {}
        initial = (data.get("initial") or "").strip()
    except Exception:
        pass
    if sys.platform != "win32":
        return jsonify({"ok": False, "error": "仅支持 Windows 平台", "path": None})
    try:
        # 用 Shell.Application 打开原生文件夹选择框（兼容性好，无需额外依赖）
        safe_initial = initial.replace("'", "''")
        ps = f"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject Shell.Application
$root = '{safe_initial}'
if (-not (Test-Path -LiteralPath $root)) {{ $root = 0 }}
$folder = $shell.BrowseForFolder(0, '选择文件夹', 0, $root)
if ($folder -ne $null) {{ [Console]::Out.Write($folder.Self.Path) }}
"""
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=600,
        )
        out = (proc.stdout or "").strip()
        if out and os.path.isabs(out):
            return jsonify({"ok": True, "path": out})
        # 用户取消或未取得路径
        return jsonify({"ok": True, "path": None})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "选择超时（超过 10 分钟）", "path": None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "path": None})


# === 日志 API ===
@app.route("/api/projects/<project_id>/logs")
def api_logs(project_id):
    """获取项目日志"""
    lines = request.args.get("lines", 300, type=int)
    content = pm.read_log(project_id, lines)
    return jsonify({"content": content})


# === 配置 API（兼容保留） ===
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(load_config())
    else:
        data = request.get_json()
        if data:
            save_config(data)
            return jsonify({"ok": True, "message": "配置已保存"})
        return jsonify({"ok": False, "message": "无效的配置数据"}), 400


@app.route("/api/check-port", methods=["POST"])
def api_check_port():
    data = request.get_json()
    port = data.get("port", 0)
    if not port:
        return jsonify({"open": False})
    return jsonify({"open": is_port_open(port)})


@app.route("/api/node-versions")
def api_node_versions():
    """读取 nvm 目录，返回已安装的 Node 版本列表"""
    nvm_home = os.environ.get("NVM_HOME", r"D:\nvm")
    versions = []
    if os.path.isdir(nvm_home):
        for name in sorted(os.listdir(nvm_home), reverse=True):
            if re.match(r'^v\d+\.\d+\.\d+$', name):
                versions.append(name[1:])  # 去掉 'v' 前缀
    return jsonify(versions)


# ============================================================
# 远程服务器 API
# ============================================================
@app.route("/api/remote-servers", methods=["GET"])
def api_remote_servers_list():
    """获取远程服务器列表"""
    config = load_config()
    return jsonify(config.get("remoteServers", []))


@app.route("/api/remote-servers", methods=["POST"])
def api_remote_servers_create():
    """添加远程服务器"""
    data = request.get_json()
    if not data or not data.get("name") or not data.get("host"):
        return jsonify({"ok": False, "message": "服务器名称和主机不能为空"}), 400

    config = load_config()
    sid = data.get("id", "").strip() or _safe_id(data["name"])

    # 检查 ID 唯一性
    for s in config.get("remoteServers", []):
        if s["id"] == sid:
            return jsonify({"ok": False, "message": f"服务器 ID '{sid}' 已存在"}), 400

    server = {
        "id": sid,
        "name": data["name"].strip(),
        "host": data["host"].strip(),
        "port": int(data.get("port", 22)),
        "username": data.get("username", "root").strip(),
        "password": data.get("password", "").strip(),
    }
    config.setdefault("remoteServers", []).append(server)
    save_config(config)
    return jsonify({"ok": True, "message": f"服务器 '{server['name']}' 已添加", "server": server})


@app.route("/api/remote-servers/<server_id>", methods=["PUT"])
def api_remote_servers_update(server_id):
    """编辑远程服务器"""
    data = request.get_json()
    config = load_config()
    servers = config.get("remoteServers", [])

    for i, s in enumerate(servers):
        if s["id"] == server_id:
            if "name" in data:
                s["name"] = data["name"].strip()
            if "host" in data:
                s["host"] = data["host"].strip()
            if "port" in data:
                s["port"] = int(data["port"])
            if "username" in data:
                s["username"] = data["username"].strip()
            if "password" in data:
                s["password"] = data["password"].strip()
            if "id" in data and data["id"] != server_id:
                new_id = _safe_id(data["id"])
                if any(x["id"] == new_id for x in servers):
                    return jsonify({"ok": False, "message": f"服务器 ID '{new_id}' 已存在"}), 400
                # 更新关联的远程项目
                for rp in config.get("remoteProjects", []):
                    if rp.get("serverId") == server_id:
                        rp["serverId"] = new_id
                s["id"] = new_id
            servers[i] = s
            config["remoteServers"] = servers
            save_config(config)
            return jsonify({"ok": True, "message": f"服务器 '{s['name']}' 已更新", "server": s})

    return jsonify({"ok": False, "message": "服务器不存在"}), 404


@app.route("/api/remote-servers/<server_id>", methods=["DELETE"])
def api_remote_servers_delete(server_id):
    """删除远程服务器"""
    config = load_config()
    config["remoteServers"] = [s for s in config.get("remoteServers", []) if s["id"] != server_id]
    # 同时删除关联的远程项目
    config["remoteProjects"] = [p for p in config.get("remoteProjects", []) if p.get("serverId") != server_id]
    save_config(config)
    return jsonify({"ok": True, "message": "服务器及其关联项目已删除"})


@app.route("/api/remote-servers/<server_id>/test", methods=["POST"])
def api_remote_servers_test(server_id):
    """测试 SSH 连接"""
    config = load_config()
    server = None
    for s in config.get("remoteServers", []):
        if s["id"] == server_id:
            server = s
            break
    if not server:
        return jsonify({"ok": False, "message": "服务器不存在"}), 404

    ok, msg = rm.test_connection(server)
    return jsonify({"ok": ok, "message": msg})


# ============================================================
# 远程项目 API
# ============================================================
@app.route("/api/remote-projects", methods=["GET"])
def api_remote_projects_list():
    """获取远程项目列表"""
    config = load_config()
    return jsonify(config.get("remoteProjects", []))


# === 框架管理 CRUD API ===
@app.route("/api/frameworks", methods=["GET"])
def api_frameworks_list():
    """获取所有框架"""
    config = load_config()
    return jsonify(config.get("frameworks", []))


@app.route("/api/frameworks", methods=["POST"])
def api_frameworks_create():
    """新增框架"""
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"ok": False, "message": "框架名称不能为空"}), 400
    config = load_config()
    frameworks = config.setdefault("frameworks", [])
    fid = data.get("id", "").strip() or _safe_id(data["name"])
    if any(f.get("id") == fid or f.get("name") == data["name"].strip() for f in frameworks):
        return jsonify({"ok": False, "message": f"框架 '{data['name'].strip()}' 已存在"}), 400
    fw = {
        "id": fid,
        "name": data["name"].strip(),
        "display_name": data.get("display_name", "").strip(),
        "icon": data.get("icon", "🧩").strip() or "🧩",
        "startCommand": data.get("startCommand", "").strip(),
        "compileCommand": data.get("compileCommand", "").strip(),
        "packageCommand": data.get("packageCommand", "").strip(),
        "artifactPath": data.get("artifactPath", "").strip(),
    }
    frameworks.append(fw)
    save_config(config)
    return jsonify({"ok": True, "message": f"框架 '{fw['name']}' 已添加", "framework": fw})


@app.route("/api/frameworks/<fw_id>", methods=["PUT"])
def api_frameworks_update(fw_id):
    """编辑框架"""
    data = request.get_json()
    config = load_config()
    frameworks = config.get("frameworks", [])
    for i, f in enumerate(frameworks):
        if f.get("id") == fw_id or f.get("name") == fw_id:
            upd = {
                "id": (data.get("id", f.get("id", fw_id)) or "").strip() or f.get("id", fw_id),
                "name": (data.get("name", f.get("name", "")) or "").strip() or f.get("name", ""),
                "display_name": (data.get("display_name", f.get("display_name", "")) or "").strip(),
                "icon": (data.get("icon", f.get("icon", "🧩")) or "").strip() or "🧩",
                "startCommand": (data.get("startCommand", f.get("startCommand", "")) or "").strip(),
                "compileCommand": (data.get("compileCommand", f.get("compileCommand", "")) or "").strip(),
                "packageCommand": (data.get("packageCommand", f.get("packageCommand", "")) or "").strip(),
                "artifactPath": (data.get("artifactPath", f.get("artifactPath", "")) or "").strip(),
            }
            frameworks[i] = upd
            config["frameworks"] = frameworks
            save_config(config)
            return jsonify({"ok": True, "message": f"框架 '{upd['name']}' 已更新", "framework": upd})
    return jsonify({"ok": False, "message": "框架不存在"}), 404


@app.route("/api/frameworks/<fw_id>", methods=["DELETE"])
def api_frameworks_delete(fw_id):
    """删除框架"""
    config = load_config()
    frameworks = config.get("frameworks", [])
    new_list = [f for f in frameworks if f.get("id") != fw_id and f.get("name") != fw_id]
    if len(new_list) == len(frameworks):
        return jsonify({"ok": False, "message": "框架不存在"}), 404
    config["frameworks"] = new_list
    save_config(config)
    return jsonify({"ok": True, "message": "框架已删除"})


@app.route("/api/remote-projects", methods=["POST"])
def api_remote_projects_create():
    """新增远程项目"""
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"ok": False, "message": "项目名称不能为空"}), 400
    if not data.get("serverId"):
        return jsonify({"ok": False, "message": "请选择所属服务器"}), 400

    config = load_config()
    pid = data.get("id", "").strip() or _safe_id(data["name"])

    for p in config.get("remoteProjects", []):
        if p["id"] == pid:
            return jsonify({"ok": False, "message": f"远程项目 ID '{pid}' 已存在"}), 400

    max_order = max((p.get("startOrder", 0) for p in config.get("remoteProjects", [])), default=0)

    project = {
        "id": pid,
        "name": data["name"].strip(),
        "serverId": data["serverId"].strip(),
        "path": data.get("path", "").strip(),
        "command": data.get("command", "").strip(),
        "compileCommand": data.get("compileCommand", "").strip(),
        "packageCommand": data.get("packageCommand", "").strip(),
        "framework": data.get("framework", "").strip(),
        "artifactPath": data.get("artifactPath", "").strip(),
        "packageTargetDir": data.get("packageTargetDir", "").strip(),
        "port": int(data.get("port", 0)),
        "enabled": data.get("enabled", True),
        "startOrder": data.get("startOrder", max_order + 1),
    }
    config.setdefault("remoteProjects", []).append(project)
    save_config(config)
    return jsonify({"ok": True, "message": f"远程项目 '{project['name']}' 已添加", "project": project})


@app.route("/api/remote-projects/<project_id>", methods=["PUT"])
def api_remote_projects_update(project_id):
    """编辑远程项目"""
    data = request.get_json()
    config = load_config()
    projects = config.get("remoteProjects", [])

    for i, p in enumerate(projects):
        if p["id"] == project_id:
            if "name" in data:
                p["name"] = data["name"].strip()
            if "serverId" in data:
                p["serverId"] = data["serverId"].strip()
            if "path" in data:
                p["path"] = data["path"].strip()
            if "command" in data:
                p["command"] = data["command"].strip()
            if "compileCommand" in data:
                p["compileCommand"] = data["compileCommand"].strip()
            if "packageCommand" in data:
                p["packageCommand"] = data["packageCommand"].strip()
            if "framework" in data:
                p["framework"] = data["framework"].strip()
            if "artifactPath" in data:
                p["artifactPath"] = data["artifactPath"].strip()
            if "packageTargetDir" in data:
                p["packageTargetDir"] = data["packageTargetDir"].strip()
            if "port" in data:
                p["port"] = int(data["port"])
            if "enabled" in data:
                p["enabled"] = bool(data["enabled"])
            if "startOrder" in data:
                p["startOrder"] = int(data["startOrder"])
            if "id" in data and data["id"] != project_id:
                new_id = _safe_id(data["id"])
                if any(x["id"] == new_id for x in projects):
                    return jsonify({"ok": False, "message": f"远程项目 ID '{new_id}' 已存在"}), 400
                p["id"] = new_id
            projects[i] = p
            config["remoteProjects"] = projects
            save_config(config)
            return jsonify({"ok": True, "message": f"远程项目 '{p['name']}' 已更新", "project": p})

    return jsonify({"ok": False, "message": "远程项目不存在"}), 404


@app.route("/api/remote-projects/<project_id>", methods=["DELETE"])
def api_remote_projects_delete(project_id):
    """删除远程项目"""
    config = load_config()
    config["remoteProjects"] = [p for p in config.get("remoteProjects", []) if p["id"] != project_id]
    save_config(config)
    return jsonify({"ok": True, "message": "远程项目已删除"})


# === 远程项目操作 API ===
def _get_remote_server_for_project(project):
    """根据项目的 serverId 获取服务器配置"""
    config = load_config()
    for s in config.get("remoteServers", []):
        if s["id"] == project.get("serverId"):
            return s
    return None


@app.route("/api/remote-projects/<project_id>/start", methods=["POST"])
def api_remote_start(project_id):
    """远程启动项目"""
    project = rm._find_project(project_id)
    if not project:
        return jsonify({"ok": False, "message": f"远程项目 {project_id} 不存在"})

    server = _get_remote_server_for_project(project)
    if not server:
        return jsonify({"ok": False, "message": "关联的远程服务器不存在"})

    ok, msg = rm.start_project(server, project)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/remote-projects/<project_id>/stop", methods=["POST"])
def api_remote_stop(project_id):
    """远程停止项目"""
    project = rm._find_project(project_id)
    if not project:
        return jsonify({"ok": False, "message": f"远程项目 {project_id} 不存在"})

    server = _get_remote_server_for_project(project)
    if not server:
        return jsonify({"ok": False, "message": "关联的远程服务器不存在"})

    ok, msg = rm.stop_project(server, project)
    return jsonify({"ok": ok, "message": msg or "已停止"})


@app.route("/api/remote-projects/<project_id>/compile", methods=["POST"])
def api_remote_compile(project_id):
    """远程编译项目"""
    project = rm._find_project(project_id)
    if not project:
        return jsonify({"ok": False, "message": f"远程项目 {project_id} 不存在"})

    server = _get_remote_server_for_project(project)
    if not server:
        return jsonify({"ok": False, "message": "关联的远程服务器不存在"})

    ok, msg = rm.compile_project(server, project)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/remote-projects/<project_id>/compiling")
def api_remote_compiling(project_id):
    """查询远程编译状态"""
    return jsonify({"compiling": rm.is_compiling(project_id)})


@app.route("/api/remote-projects/<project_id>/package", methods=["POST"])
def api_remote_package(project_id):
    """远程打包项目（先构建再复制产物）"""
    project = rm._find_project(project_id)
    if not project:
        return jsonify({"ok": False, "message": f"远程项目 {project_id} 不存在"})

    server = _get_remote_server_for_project(project)
    if not server:
        return jsonify({"ok": False, "message": "关联的远程服务器不存在"})

    ok, msg = rm.package_project(server, project)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/remote-projects/<project_id>/logs")
def api_remote_logs(project_id):
    """读取远程日志"""
    project = rm._find_project(project_id)
    if not project:
        return jsonify({"content": "远程项目不存在"})

    server = _get_remote_server_for_project(project)
    if not server:
        return jsonify({"content": "关联的远程服务器不存在"})

    lines = request.args.get("lines", 400, type=int)
    content = rm.read_log(server, project, lines)
    return jsonify({"content": content})


@app.route("/api/remote-status")
def api_remote_status():
    """获取所有远程项目状态"""
    config = load_config()
    result = {}
    for p in config.get("remoteProjects", []):
        server = None
        for s in config.get("remoteServers", []):
            if s["id"] == p.get("serverId"):
                server = s
                break
        if server:
            status = rm.get_status(server, p)
        else:
            status = "stopped"
        result[p["id"]] = {
            "status": status,
            "port": p["port"],
            "name": p["name"],
            "serverName": server["name"] if server else "未知服务器",
            "serverHost": server["host"] if server else "",
        }
    return jsonify(result)


@app.route("/api/remote-start-all", methods=["POST"])
def api_remote_start_all():
    """一键启动所有远程项目"""
    config = load_config()
    projects = sorted(
        [p for p in config.get("remoteProjects", []) if p.get("enabled", True)],
        key=lambda p: p.get("startOrder", 999),
    )

    if not projects:
        return jsonify({"ok": False, "message": "没有可启动的远程项目"})

    started = []
    for project in projects:
        server = None
        for s in config.get("remoteServers", []):
            if s["id"] == project.get("serverId"):
                server = s
                break
        if not server:
            continue

        pid = project["id"]
        name = project["name"]

        if rm.get_status(server, project) == "running":
            started.append(name)
            continue

        ok, msg = rm.start_project(server, project)
        if not ok:
            return jsonify({"ok": False, "message": f"{name} 启动失败: {msg}", "started": started})

        # 等待当前项目就绪
        port = project.get("port", 0)
        if port:
            ready = False
            for _ in range(180):
                time.sleep(1)
                status = rm.get_status(server, project)
                if status == "running":
                    ready = True
                    break
            if not ready:
                return jsonify({"ok": False, "message": f"{name} 启动超时（3分钟），请检查日志", "started": started})
        else:
            time.sleep(3)

        started.append(name)

    return jsonify({
        "ok": True,
        "message": f"已启动: {', '.join(started)}" if started else "所有远程项目已在运行中",
        "started": started,
    })


@app.route("/api/remote-stop-all", methods=["POST"])
def api_remote_stop_all():
    """一键停止所有远程项目"""
    config = load_config()
    projects = sorted(
        config.get("remoteProjects", []),
        key=lambda p: p.get("startOrder", 999),
        reverse=True,
    )

    results = {}
    for project in projects:
        server = None
        for s in config.get("remoteServers", []):
            if s["id"] == project.get("serverId"):
                server = s
                break
        if not server:
            continue

        pid = project["id"]
        ok, msg = rm.stop_project(server, project)
        results[pid] = {"ok": ok, "message": msg or "已停止"}

    return jsonify(results)



# ============================================================
# 清理 & 启动
# ============================================================
def cleanup():
    """退出时清理所有子进程"""
    config = load_config()
    for p in config.get("projects", []):
        pm.stop(p["id"])
    # 停止远程项目
    for p in config.get("remoteProjects", []):
        server = None
        for s in config.get("remoteServers", []):
            if s["id"] == p.get("serverId"):
                server = s
                break
        if server:
            try:
                rm.stop_project(server, p)
            except Exception:
                pass


atexit.register(cleanup)


def signal_handler(sig, frame):
    cleanup()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Project Launcher")
    parser.add_argument("--host", help="监听地址 (默认取配置 server.host，否则 0.0.0.0)")
    parser.add_argument("--port", type=int, help="监听端口 (默认取配置 server.port)")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    config = load_config()
    server_cfg = config.get("server", {}) or {}
    server_port = args.port or server_cfg.get("port", 5150)
    server_host = args.host or server_cfg.get("host", "0.0.0.0")

    project_count = len(config.get("projects", []))
    banner = (
        "=" * 46 + "\n"
        f"  Project Launcher v2.0\n"
        f"  URL:   http://{server_host}:{server_port}\n"
        f"  Projects: {project_count}\n"
        f"  Press Ctrl+C to exit\n"
        + "=" * 46
    )
    print(banner)

    if not args.no_browser and sys.platform == "win32":
        try:
            import webbrowser
            threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{server_port}")).start()
        except Exception:
            pass

    app.run(host=server_host, port=server_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
