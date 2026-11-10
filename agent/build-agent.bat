@echo off
cd /d "%~dp0"
pip install pyinstaller
pyinstaller --onefile --name MiniRemoteAgent --hidden-import win32timezone agent.py
echo.
echo Built: %~dp0dist\MiniRemoteAgent.exe
pause
