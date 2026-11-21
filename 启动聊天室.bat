@echo off
cd /d %~dp0
title 局域网聊天室 - 服务器（关闭此窗口就停止服务）
echo ============================================
echo   正在启动聊天室服务器，端口 9999
echo   启动后会自动打开浏览器
echo   关闭这个黑窗口 = 停止服务
echo ============================================
start "" cmd /c "ping -n 2 127.0.0.1 >nul & start http://127.0.0.1:9999"
.venv\Scripts\python.exe server.py
pause
