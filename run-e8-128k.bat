@echo off
REM ============================================================
REM  NInfer-4090 方案B 一键启动：E8 @ 128K（日常推荐档）
REM  4-bit E8 KV，MTP3 投机解码，OpenAI/Anthropic 兼容 API @ 127.0.0.1:8080
REM ============================================================
setlocal
set "VCVARS=D:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "MODEL=E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer"
cd /d "%~dp0"

echo [启动] E8 @ 128K  MTP3  端口 8080
call "%VCVARS%" >nul
build-ninja\apps\ninfer-serve.exe "%MODEL%" ^
  --kv-dtype rk4v4-e8 ^
  --spec mtp --draft-tokens 3 --lm-head-draft ^
  --max-context 131072 ^
  --host 127.0.0.1 --port 8080 ^
  --preserve-thinking

endlocal
