@echo off
rem ninfer-launcher 构建入口：双击本文件即可打包。
rem 可选参数透传给 build.ps1：build.bat -Clean / build.bat -Clean -Launch
chcp 65001 >nul
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build.ps1" %*
set RC=%ERRORLEVEL%

echo.
if %RC% equ 0 (
    echo 打包完成，产物位于：
    echo   GUI 版：dist\ninfer-launcher\ninfer-launcher.exe
    echo   CLI 版：dist\ninfer-launcher-cli\ninfer-launcher-cli.exe
) else (
    echo 打包失败（错误码 %RC%），请检查上方日志
)
pause