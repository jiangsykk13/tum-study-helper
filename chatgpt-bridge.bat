@echo off
rem TUM Moodle helper - read-only tools for ChatGPT via OpenAI Secure MCP Tunnel.
rem Close this window to disconnect ChatGPT.
chcp 65001 >nul
title TUM Moodle for ChatGPT (read-only) - close to disconnect
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%APPDATA%\uv\tools\pymoodlekit\Scripts\python.exe"
if not exist "%PY%" (
  echo Python environment not found. Run the installation steps in README.md first.
  pause
  exit /b 1
)
"%PY%" -I -B "%~dp0tum_moodle_auto.py" chatgpt
if errorlevel 1 pause
