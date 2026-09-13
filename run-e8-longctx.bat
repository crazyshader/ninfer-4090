@echo off
REM ============================================================
REM  NInfer-4090 方案B 一键启动：E8 @ 320K（超长上下文档）
REM  4-bit E8 KV + MTP4，适合长文档/RAG/大代码库
REM  README 标称 E8 4-bit 满配可达 433K；此处取 320K 稳妥留余量
REM  若显存吃紧可把 --max-context 调小，或换 rk2v4-e8（2-bit）冲更长
REM ============================================================
setlocal
set "VCVARS=D:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "MODEL=E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer"
cd /d "%~dp0"

echo [启动] E8 @ 320K  MTP4  端口 8080  （超长上下文，冷启后耐心等待）
call "%VCVARS%" >nul
build-ninja\apps\ninfer-serve.exe "%MODEL%" ^
  --kv-dtype rk4v4-e8 ^
  --spec mtp --draft-tokens 4 --lm-head-draft ^
  --max-context 320000 ^
  --host 127.0.0.1 --port 8080 ^
  --preserve-thinking

endlocal
