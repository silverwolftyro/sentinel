@echo off
chcp 65001 >nul 2>&1
rem 刻意**不开** enabledelayedexpansion：
rem 延迟扩展会把 `!` 当成变量定界符，下面的 `[!]` 会被吃掉成 `[]`。
rem 本脚本里的变量都在块外赋值，用不着延迟扩展。
setlocal

rem ============================================================
rem  哨兵 Sentinel —— Windows 一键启动器
rem
rem  用法：
rem    启动哨兵.bat                       无参数 → 交互菜单（演示 / 雷达 / 接管）
rem    启动哨兵.bat --agent codex "写个 hello"
rem    启动哨兵.bat --session-file logs\sessions.json
rem
rem  会自动：
rem    1. 切到项目根目录；
rem    2. 优先用 .venv\Scripts\python.exe，其次 venv\，再退到 py -3 / 系统 Python；
rem    3. 装好依赖（缺什么装什么，只装一次）；
rem    4. 拉起哨兵，并把全部参数原样传过去；
rem    5. 无论成功失败，最后都 pause —— 窗口**绝不闪退**。
rem ============================================================

cd /d "%~dp0"
set "ROOT=%CD%"

echo.
echo ========================================
echo   🛡️ 哨兵系统正在启动...
echo ========================================
echo.

rem ---- 选 Python（venv 优先，其次系统）----
set "PYEXE="
set "PYARGS="
if exist "%ROOT%\.venv\Scripts\python.exe" set "PYEXE=%ROOT%\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOT%\venv\Scripts\python.exe" set "PYEXE=%ROOT%\venv\Scripts\python.exe"
if not defined PYEXE (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
    )
)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE goto :no_python
echo   [OK] Python：%PYEXE% %PYARGS%

rem ---- 依赖自检（只装一次）----
%PYEXE% %PYARGS% -c "import fastapi, uvicorn, requests" >nul 2>&1
if errorlevel 1 (
    echo   [..] 正在安装依赖（只需一次）…
    %PYEXE% %PYARGS% -m pip install -r "%ROOT%\_构建与发布\requirements.txt"
    if errorlevel 1 goto :dep_failed
    echo   [OK] 依赖就绪
) else (
    echo   [OK] 依赖已就绪
)

rem ---- 启动 ----
echo.
if "%~1"=="" (
    rem ⚠️ 无参数 = **先问用户想干什么**（v3.0.2）。
    rem 以前这里是直接进演示模式 —— 用户以为「雷达扫不到真实的进程」。
    rem 现在走入口脚本，终端里会打印三个选项：
    rem   [1] 启动演示模式  [2] 开启雷达  [3] 接管并启动新的 Agent
    echo   [..] 未检测到参数 —— 接下来会让你选要做什么。
    echo.
    "%PYEXE%" %PYARGS% "%ROOT%\_构建与发布\scripts\sentinel_entry.py"
    goto :finish
)

echo   [..] 正在启动哨兵 …（Ctrl+C 退出）
echo.
"%PYEXE%" %PYARGS% "%ROOT%\_构建与发布\scripts\run_with_sentinel.py" %*
goto :finish

rem ============================================================
rem  失败出口：都给出**看得懂**的中文提示
rem ============================================================
:no_python
echo.
echo   [X] 未找到 Python，请先安装。
echo       · 装 Python 3.10 或更高版本，并勾选「Add to PATH」
echo       · 或者在项目根目录建一个 .venv 虚拟环境
echo.
set "EXITCODE=1"
goto :finish

:dep_failed
echo.
echo   [X] 依赖安装失败，请手动执行：
echo         pip install -r "_构建与发布\requirements.txt"
echo.
set "EXITCODE=1"
goto :finish

rem ============================================================
rem  统一出口：**无论成功还是失败都 pause**，窗口不会瞬间消失
rem ============================================================
:finish
echo.
if not defined EXITCODE set "EXITCODE=%ERRORLEVEL%"
if "%EXITCODE%"=="0" (
    echo   [OK] 哨兵已退出，本次审查未发现告警。
) else (
    echo   [!] 哨兵已退出（退出码 %EXITCODE%）—— 详情见看板与 logs\ 目录。
)
echo.
echo   按任意键关闭本窗口…
pause >nul
exit /b %EXITCODE%
