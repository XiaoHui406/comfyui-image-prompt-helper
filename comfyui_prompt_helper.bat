@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Prefer the project virtualenv (created by `uv sync`), so that the dependency
rem versions match pyproject.toml / uv.lock. Fall back to the global python on
rem PATH only when .venv is missing, and say so.
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PY_LABEL=project venv .venv"

if exist "%PY%" goto check_deps

set "PY=python"
set "PYW=pythonw"
set "PY_LABEL=global python on PATH (no .venv found)"
echo [!] Virtualenv not found: %~dp0.venv
echo     Falling back to the global python.
echo     To get the pinned dependencies run:  uv sync
echo.

:check_deps
rem Dependency self-check: if it fails, keep the window open with the real error
"%PY%" -c "import tkinter, PIL, openai" 2>nul
if not errorlevel 1 goto launch

echo [!] Missing dependencies in %PY_LABEL%
echo     Interpreter: %PY%
echo     Fix one of these:
echo         1) uv sync                     (installs into .venv, recommended)
echo         2) pip install pillow openai   (installs into that interpreter)
echo.
"%PY%" -c "import tkinter, PIL, openai"
pause
exit /b 1

:launch
rem launch the GUI without a console window
start "" "%PYW%" "comfyui_prompt_helper.py"
