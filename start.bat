@echo off
chcp 65001 >nul
title 芋道启动器

set SCRIPT=%~dp0app.py

where python >nul 2>nul
if errorlevel 1 (
    echo 未找到 Python，请先激活虚拟环境或 Conda 环境
    pause
    exit /b 1
)

echo 正在启动芋道启动器...
python "%SCRIPT%"
pause
