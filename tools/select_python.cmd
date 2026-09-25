@echo off
rem Select a Python environment belonging to this Windows computer.
rem Variables intentionally remain available to the calling .cmd file.
for %%I in ("%~dp0..") do set "BOT_PROJECT_ROOT=%%~fI"
set "BOT_VENV=%BOT_PROJECT_ROOT%\.venvs\%COMPUTERNAME%"
set "BOT_PYTHON=%BOT_VENV%\Scripts\python.exe"
set "BOT_PYTHONW=%BOT_VENV%\Scripts\pythonw.exe"
if exist "%BOT_PYTHON%" exit /b 0

rem Backward-compatible environment used on the original computer.
set "BOT_VENV=%BOT_PROJECT_ROOT%\.venv"
set "BOT_PYTHON=%BOT_VENV%\Scripts\python.exe"
set "BOT_PYTHONW=%BOT_VENV%\Scripts\pythonw.exe"
if exist "%BOT_PYTHON%" exit /b 0
exit /b 1
