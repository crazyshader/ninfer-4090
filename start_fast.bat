@echo off
REM ============================================================
REM  NInfer-4090  Qwen3.8-27B  -  FAST MODE (no thinking)
REM  For everyday coding. Fastest decode.
REM  Double-click to start. Press Ctrl+C to stop, close window to exit.
REM ============================================================
title NInfer-4090 [FAST mode / no-thinking]

call "D:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
set "PATH=E:\ai\ninfer-4090-native\build-ninja\apps;%PATH%"
cd /d E:\ai\ninfer-4090-native

echo Starting NInfer-4090 in FAST mode (no-thinking)...
echo API will be at http://127.0.0.1:8080/v1
echo.

build-ninja\apps\ninfer-serve.exe "E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer" ^
  --host 127.0.0.1 --port 8080 ^
  --kv-dtype rk4v4-e8 ^
  --spec mtp --draft-tokens 7 --lm-head-draft ^
  --max-context 163840 ^
  --prefill-chunk 1024 ^
  --no-thinking

echo.
echo === server stopped (exit code %errorlevel%) ===
pause
