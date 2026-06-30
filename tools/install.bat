@echo off
net session >nul 2>&1
if %errorlevel% equ 0 goto :run

powershell -Command "Start-Process cmd -Verb RunAs -ArgumentList '/c \"\"%~f0\"\" %*'"
exit /b

:run
powershell -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
pause
