@echo off
setlocal

set "DEFAULT_HOST=192.168.50.216"
set "PYTHON_EXE=%~dp0python-3.11.0\python.exe"
set "SCRIPT_DIR=%~dp0hl2ss-lk\viewer"
set "SCRIPT_NAME=lk_multimodal_dataset_capture.py"
set "DEFAULT_ROOM=None"

set /p ROOM_NAME="Enter the Room Name: "
if "!ROOM_NAME!"=="" set ROOM_NAME=%DEFAULT_ROOM%

set /p HOST_IP="Enter Host IP: (Or using defult %DEFAULT_HOST%)"
if "%HOST_IP%"=="" set HOST_IP=%DEFAULT_HOST%

echo.
echo Starting...
echo Python Path: %PYTHON_EXE%
echo Script: %SCRIPT_DIR%\%SCRIPT_NAME%
echo HOST IP: %HOST_IP%
echo.

cd /d "%SCRIPT_DIR%"

"%PYTHON_EXE%" "%SCRIPT_NAME%" --roomname "%ROOM_NAME%" --nrec 6 --host %HOST_IP%

echo.
echo DONE.
pause