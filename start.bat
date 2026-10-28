@echo off
chcp 936 >nul 2>nul
cd /d "%~dp0"
title GDMEC 课表 - 本地服务

echo ================================================
echo   广东机电职业技术学院 · 课表代理
echo   直接使用系统 Python，不需要虚拟环境
echo   手机同网段访问：start.bat lan
echo ================================================
echo.

rem ========== 1. 查找可用的 Python ==========
set "PY="

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=python"
    )
)

if not defined PY if exist "D:\APP\Python313\python.exe" set "PY=D:\APP\Python313\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python313\python.exe" set "PY=%ProgramFiles%\Python313\python.exe"

if not defined PY goto nopython

echo [环境] 解释器 %PY%
%PY% -c "import sys;print('[环境] 版本', sys.version.split()[0])"

rem ========== 2. 检查依赖，缺了才装 ==========
%PY% -c "import fastapi, uvicorn, httpx" >nul 2>nul
if errorlevel 1 (
    echo [依赖] 缺少 fastapi / uvicorn / httpx，正在安装...
    %PY% -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 goto pipfail
    echo [依赖] 安装完成
) else (
    echo [依赖] 已就绪
)

rem ========== 3. 找空闲端口 8000-8010 ==========
set /a TRY=8000
:findport
netstat -ano | findstr /C:":%TRY% " | findstr /C:"LISTENING" >nul
if errorlevel 1 goto gotport
set /a TRY+=1
if %TRY% LEQ 8010 goto findport
echo [错误] 8000-8010 全部被占用，请先关闭占用程序。
pause
exit /b 1
:gotport
set "PORT=%TRY%"

set "HOST=127.0.0.1"
if /i "%~1"=="lan" set "HOST=0.0.0.0"

echo.
echo [启动] 打开 http://127.0.0.1:%PORT%
if /i "%~1"=="lan" echo [局域网] 监听 0.0.0.0，手机用「本机IP:%PORT%」访问
echo [提示] 关闭本窗口即停止服务
echo.

start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:%PORT%'"

%PY% -m uvicorn backend.app:app --host %HOST% --port %PORT%

echo.
echo 服务已停止。
pause
exit /b 0

:nopython
echo [错误] 没找到可用的 Python，请安装 Python 3.10+ 并勾选 Add Python to PATH。
echo        https://www.python.org/downloads/
pause
exit /b 1

:pipfail
echo [错误] 依赖安装失败，请检查网络后手动执行：
echo        %PY% -m pip install -r requirements.txt
echo        国内可用镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b 1
