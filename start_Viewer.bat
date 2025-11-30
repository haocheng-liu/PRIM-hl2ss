@echo off
setlocal

set "DEFAULT_HOST=192.168.1.36"
set "PYTHON_EXE=%~dp0python-3.11.0\python.exe"
set "SCRIPT_DIR=%~dp0prim_viewer"
set "SCRIPT_NAME=main.py"
set "DEFAULT_ROOM=None"

echo.
echo Starting...
echo Python Path: %PYTHON_EXE%
echo Script: %SCRIPT_DIR%\%SCRIPT_NAME%
echo.

cd /d "%SCRIPT_DIR%"

"%PYTHON_EXE%" "%SCRIPT_NAME%"

echo.
echo DONE.
pause
