@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0build_share.py"
) else (
  python "%~dp0build_share.py"
)
if errorlevel 1 pause
