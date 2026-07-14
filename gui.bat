@echo off
setlocal
title Power Analyser
rem Always run from this script's folder (the project root)
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Virtual environment not found: %PY%
    echo Run the installation steps in README.md first ^(python -m venv .venv, etc.^)
    pause
    exit /b 1
)

"%PY%" -m power_analyser.gui.app %*

if errorlevel 1 (
    echo.
    echo [ERROR] Power Analyser exited with an error.
    pause
)

endlocal
