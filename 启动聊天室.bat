@echo off
cd /d %~dp0
echo ============================================
echo   Chat room server starting on port 9999...
echo   Press Ctrl+C to stop.
echo ============================================
start "" http://127.0.0.1:9999
.venv\Scripts\python.exe server.py
pause
