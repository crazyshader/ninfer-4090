@echo off
REM ============================================================
REM  NInfer-4090 方案B 一键启动：INT8 @ 128K（最高数值精度档）
REM  KV 不压缩，MTP3 投机解码，API @ 127.0.0.1:8080
REM ============================================================
setlocal
set "VCVARS=D:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "MODEL=E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer"
cd /d "%~dp0"

echo [启动] INT8 @ 128K  MTP3  端口 8080
call "%VCVARS%" >nul
build-ninja\apps\ninfer-serve.exe "%MODEL%" ^
  --kv-dtype int8 ^
  --spec mtp --draft-tokens 3 --lm-head-draft ^
  --max-context 131072 ^
  --host 127.0.0.1 --port 8080 ^
  --preserve-thinking

endlocal
