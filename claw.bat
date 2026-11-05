@chcp 65001 >nul
@echo off
title OpenClaw 服务管理 (Gateway+8199 中转, 手动运行=重启所有服务)
setlocal enabledelayedexpansion

rem ===== 自提权（服务操作需要管理员权限）=====
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 需要管理员权限, 正在请求提升...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "NSSM=%~dp0nssm.exe"
set "SVC1=OpenClawGateway"
set "SVC2=OpenClawProxy"
rem OpenClaw 的 state/agent-node 归属 M9-Z2 用户（实测确认, 勿按当前 shell 用户推断）
set "OC_USER=C:\Users\M9-Z2"
set "OC_STATE=%OC_USER%\.openclaw"
set "BASE=%~dp0"
rem 去尾反斜杠: 避免 %BASE% 拼接时 \" 被 cmd 解析为转义引号
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

echo ============================================
echo  OpenClaw 服务管理 Gateway+8199 中转
echo  开机自启已由服务注册, 无需登录
echo  手动运行本脚本=重启所有服务
echo ============================================
echo.

echo [1/5] 停止所有服务...
%NSSM% stop %SVC1% >nul 2>&1
%NSSM% stop %SVC2% >nul 2>&1
call openclaw gateway stop --force >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":58497 .*LISTENING"') do taskkill /F /PID %%P >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /C:":8199" ^| findstr /C:"LISTENING"') do taskkill /F /PID %%P >nul 2>&1
taskkill /F /IM openclaw-session-host.exe >nul 2>&1
ping -n 3 127.0.0.1 >nul
echo   已停止 (Gateway / 中转服务 / 残留进程清理完毕)。

echo.
echo [2/5] 微信渠道配置检查...
call openclaw channels list 2>nul | findstr /I "weixin" >nul
if %errorlevel%==0 (
    echo   微信渠道已配置。
) else (
    echo   [微信] 插件已启用但未配置账号, 正在调起扫码登录窗口...
    echo   请直接扫描窗口中的二维码（完成后脚本自动继续, 与其他输出混排无妨）。
    call openclaw channels login --channel openclaw-weixin
    call openclaw channels list 2>nul | findstr /I "weixin" >nul
    if !errorlevel!==0 (
        echo   [OK] 微信渠道已配置完成, 开机自动生效。
    ) else (
        echo   [提示] 微信仍未配置。可稍后手动运行: openclaw channels login --channel openclaw-weixin
    )
)

echo.
echo [3/5] 更新 Gateway 服务 (OpenClawGateway)...
set "APPDIR="
for /d %%D in ("C:\Program Files\WindowsApps\OpenClawFoundation.OpenClawGateway_*") do set "APPDIR=%%D"
set "NODEDIR="
for /d %%D in ("%OC_USER%\AppData\Local\OpenClawGatewayMSIX\agent-node\node-v*") do set "NODEDIR=%%D"
if not exist "%NODEDIR%\node.exe" (
    echo   [错误] 未找到 OpenClaw node 运行时: %NODEDIR%
    pause
    exit /b 1
)
if not exist "%APPDIR%\app\openclaw.mjs" (
    echo   [错误] 未找到 openclaw.mjs: %APPDIR%
    pause
    exit /b 1
)
rem file URL 中空格转 %20（bat 里 %% 转义）
set "APPURL=%APPDIR:\=/%"
set "APPURL=!APPURL: =%%20!"

sc query %SVC1% >nul 2>&1
if %errorlevel% neq 0 (
    %NSSM% install %SVC1% "%NODEDIR%\node.exe" >nul
    echo   服务已安装。
) else (
    echo   服务已存在, 更新参数。
)
%NSSM% set %SVC1% Application "%NODEDIR%\node.exe" >nul
%NSSM% set %SVC1% AppParameters "--import file:///!APPURL!/node/native-redirect.mjs \"%APPDIR%\app\openclaw.mjs\" gateway" >nul
%NSSM% set %SVC1% AppDirectory "%OC_STATE%" >nul
%NSSM% set %SVC1% AppEnvironmentExtra "OPENCLAW_STATE_DIR=%OC_STATE%" "USERPROFILE=%OC_USER%" "APPDATA=%OC_USER%\AppData\Roaming" "TEMP=%OC_USER%\AppData\Local\Temp" >nul
%NSSM% set %SVC1% AppStdout "%OC_STATE%\nssm-gateway-out.log" >nul
%NSSM% set %SVC1% AppStderr "%OC_STATE%\nssm-gateway-err.log" >nul
%NSSM% set %SVC1% AppRotateFiles 1 >nul
%NSSM% set %SVC1% AppRotateOnline 1 >nul
%NSSM% set %SVC1% AppRotateBytes 10485760 >nul
%NSSM% set %SVC1% Start SERVICE_AUTO_START >nul
%NSSM% set %SVC1% AppRestartDelay 5000 >nul
echo   Gateway 参数已更新。

echo.
echo [4/5] 更新中转服务 (OpenClawProxy: 8199)...
rem Proxy 路径无空格: nssm 全部用裸参数, 避开 cmd 引号转义坑
sc query %SVC2% >nul 2>&1
if %errorlevel% neq 0 (
    %NSSM% install %SVC2% %BASE%\venv\Scripts\python.exe main.py >nul
    echo   服务已安装。
) else (
    echo   服务已存在, 更新参数。
)
%NSSM% set %SVC2% Application %BASE%\venv\Scripts\python.exe >nul
%NSSM% set %SVC2% AppParameters main.py >nul
%NSSM% set %SVC2% AppDirectory %BASE% >nul
%NSSM% set %SVC2% AppEnvironmentExtra APP_PORT=8199 >nul
rem nssm 自己打开日志文件, 目录必须先存在 (log/ 不入库, 新机器上不会被创建)
if not exist "%BASE%\log\supervisor" mkdir "%BASE%\log\supervisor" >nul 2>&1
%NSSM% set %SVC2% AppStdout %BASE%\log\supervisor\openclaw-out.log >nul
%NSSM% set %SVC2% AppStderr %BASE%\log\supervisor\openclaw-err.log >nul
%NSSM% set %SVC2% AppRotateFiles 1 >nul
%NSSM% set %SVC2% AppRotateOnline 1 >nul
%NSSM% set %SVC2% AppRotateBytes 10485760 >nul
%NSSM% set %SVC2% Start SERVICE_AUTO_START >nul
%NSSM% set %SVC2% AppRestartDelay 5000 >nul
echo   中转服务参数已更新。

echo.
echo [5/5] 启动所有服务并确认状态...
%NSSM% start %SVC2% >nul 2>&1
%NSSM% start %SVC1% >nul 2>&1
echo   等待服务就绪 (Gateway 首次启动较慢属正常)...
ping -n 10 127.0.0.1 >nul

set "HEALTH=000"
for /f %%H in ('curl -s -o nul --max-time 5 -w "%%{http_code}" http://127.0.0.1:8199/health 2^>nul') do set "HEALTH=%%H"
if "%HEALTH%"=="000" (
    echo   [警告] 中转服务 8199 未就绪 (服务会自动重试)
) else (
    echo   [OK] 中转服务 8199 正常 (HTTP %HEALTH%)
)
call openclaw gateway status 2>nul | findstr /C:"Connectivity check: ok" >nul
if %errorlevel%==0 (
    echo   [OK] Gateway 运行中 (Connectivity: ok, 面板 http://127.0.0.1:58497/)
) else (
    echo   [等待] Gateway 尚未就绪 (服务会自动完成启动, 稍后复测即可):
    call openclaw gateway status 2>nul | findstr /C:"Connectivity"
)

echo.
echo ============================================
echo  服务清单 (开机自启 SYSTEM 账号, 无需登录):
sc qc %SVC1% 2>nul | findstr /C:"START_TYPE"
sc qc %SVC2% 2>nul | findstr /C:"START_TYPE"
echo  Gateway 日志: %OC_STATE%\nssm-gateway-out.log
echo  中转日志:     %BASE%\log\supervisor\openclaw-out.log
echo ============================================
pause
