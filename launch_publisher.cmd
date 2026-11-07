@echo off
cd /d "%~dp0"
"%~dp0gui\.venv\Scripts\python.exe" "%~dp0packaging\publisher_gui.py"
if errorlevel 1 pause
