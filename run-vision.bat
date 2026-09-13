@echo off
REM ============================================================
REM  NInfer-4090 方案B 一键启动：Vision 多模态 @ 280K
REM  开图像/视频输入，4-bit E8 KV + MTP4，端口 8080
REM  仅在真正需要处理图片/视频时用（纯文本场景比上面几档慢）
REM ============================================================
setlocal
set "VCVARS=D:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "MODEL=E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer"
cd /d "%~dp0"

echo [启动] Vision @ 280K  MTP4  端口 8080
call "%VCVARS%" >nul
build-ninja\apps\ninfer-serve.exe "%MODEL%" ^
  --vision ^
  --kv-dtype rk4v4-e8 ^
  --spec mtp --draft-tokens 4 --lm-head-draft ^
  --max-context 280000 ^
  --host 127.0.0.1 --port 8080 ^
  --preserve-thinking

endlocal
