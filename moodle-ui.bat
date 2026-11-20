@echo off
rem TUM Moodle helper - local web UI. Close this window to quit.
chcp 65001 >nul
title TUM Moodle Helper
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%APPDATA%\uv\tools\pymoodlekit\Scripts\python.exe"
if not exist "%PY%" (
  echo Python environment not found. Run the installation steps in README.md first.
  pause
  exit /b 1
)
"%PY%" -I -B "%~dp0tum_moodle_auto.py" ui %*
if errorlevel 1 pause
