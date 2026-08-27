"""Expose YudaoLauncher's existing HTTP API as MCP tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP


LAUNCHER_URL = os.environ.get("LAUNCHER_URL", "http://127.0.0.1:5150").rstrip("/")
PUBLIC_HOST = os.environ.get("LAUNCHER_PUBLIC_HOST", "").strip()
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "5151"))

mcp = FastMCP(
    "YudaoLauncher",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    json_response=True,
)


def _request(path: str, method: str = "GET") -> Any:
    req = urllib.request.Request(
        f"{LAUNCHER_URL}{path}", method=method, headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Launcher API 返回 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Launcher ({LAUNCHER_URL}): {exc.reason}") from exc


def _projects() -> list[dict[str, Any]]:
    projects = _request("/api/projects")
    if not isinstance(projects, list):
        raise RuntimeError("Launcher 返回了无效的项目列表")
    return projects


def _find_project(name: str) -> dict[str, Any]:
    wanted = name.strip().casefold()
    projects = _projects()
    exact = [p for p in projects if str(p.get("id", "")).casefold() == wanted
             or str(p.get("name", "")).casefold() == wanted]
    if len(exact) == 1:
        return exact[0]

    partial = [p for p in projects if wanted in str(p.get("id", "")).casefold()
               or wanted in str(p.get("name", "")).casefold()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        choices = "、".join(str(p.get("name")) for p in partial)
        raise ValueError(f"项目名称不唯一，请使用更完整的名称：{choices}")
    raise ValueError(f"找不到项目：{name}")


def _public_host() -> str:
    if PUBLIC_HOST:
        return PUBLIC_HOST
    host = urllib.parse.urlparse(LAUNCHER_URL).hostname or "127.0.0.1"
    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::"}:
        return "<Linux服务器局域网IP>"
    return host


@mcp.tool()
def list_projects() -> list[dict[str, Any]]:
    """列出可操作的本地项目，包括名称、ID、端口和当前状态。"""
    statuses = _request("/api/status")
    return [{
        "id": p.get("id"), "name": p.get("name"), "port": p.get("port", 0),
        "enabled": p.get("enabled", True),
        "status": statuses.get(p.get("id"), {}).get("status", "unknown"),
    } for p in _projects()]


@mcp.tool()
def compile_project(project_name: str) -> dict[str, Any]:
    """按项目名称或 ID 开始编译项目；编译在后台异步执行。"""
    project = _find_project(project_name)
    project_id = urllib.parse.quote(str(project["id"]), safe="")
    result = _request(f"/api/projects/{project_id}/compile", "POST")
    return {**result, "project": project.get("name"), "asynchronous": True}


@mcp.tool()
def start_project(project_name: str) -> dict[str, Any]:
    """按项目名称或 ID 启动项目。"""
    project = _find_project(project_name)
    project_id = urllib.parse.quote(str(project["id"]), safe="")
    result = _request(f"/api/projects/{project_id}/start", "POST")
    return {**result, "project": project.get("name")}


@mcp.tool()
def open_project(project_name: str) -> dict[str, Any]:
    """获取项目的局域网访问地址；不会在 Linux 服务器上弹浏览器。"""
    project = _find_project(project_name)
    port = int(project.get("port") or 0)
    if not port:
        raise ValueError(f"项目 {project.get('name')} 没有配置访问端口")
    status = _request("/api/status").get(project["id"], {}).get("status", "unknown")
    return {
        "ok": True, "project": project.get("name"), "status": status,
        "url": f"http://{_public_host()}:{port}",
        "message": "请在 Agent 所在设备的浏览器中打开该地址",
    }


@mcp.tool()
def project_status(project_name: str) -> dict[str, Any]:
    """查询指定项目的运行状态和编译状态。"""
    project = _find_project(project_name)
    project_id = urllib.parse.quote(str(project["id"]), safe="")
    status = _request("/api/status").get(project["id"], {})
    compiling = _request(f"/api/projects/{project_id}/compiling").get("compiling", False)
    return {"project": project.get("name"), **status, "compiling": compiling}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
