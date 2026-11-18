@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Demonio ATAS V5.1 - Registro_bot_V5, pestana V5
cd /d "%~dp0"
"C:\Users\USUARIO\AppData\Local\Programs\Python\Python312\python.exe" "%~dp0demonio_v4.py"
echo.
echo Demonio V5 cerrado.
pause
