@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 goto :try_python
py -3 "%~dp0app\launcher.py"
exit /b %ERRORLEVEL%

:try_python
where python >nul 2>&1
if errorlevel 1 goto :python_missing
python "%~dp0app\launcher.py"
exit /b %ERRORLEVEL%

:python_missing
echo 未找到 Python，请先安装 Python 3。
echo.
pause
exit /b 1
