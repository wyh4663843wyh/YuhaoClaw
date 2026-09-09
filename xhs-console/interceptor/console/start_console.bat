@echo off
title Xiaohongshu Console
echo ============================================
echo   Xiaohongshu Console  -  starting
echo ============================================
echo.

cd /d "%~dp0"

rem Use system python; if you use a venv, point PY to your python.exe
set "PY=python"
set PORT=8090

echo Server: http://127.0.0.1:%PORT%
echo Keep this window open.  Ctrl+C to stop.
echo.

"%PY%" server.py --port %PORT%

echo.
echo Service exited.
pause
