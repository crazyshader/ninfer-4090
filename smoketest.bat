@echo off
REM ============================================================
REM  冒烟测试：向已启动的 ninfer-serve 发一条请求，验证 API 通
REM  用法：先双击某个 run-*.bat 起服务，等出现 listening 后，
REM        再双击本脚本。
REM ============================================================
setlocal
echo [测试] 向 http://127.0.0.1:8080/v1/chat/completions 发一条请求...
echo.
curl -s http://127.0.0.1:8080/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"qwen3.8-27b\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK if you can read this.\"}],\"max_tokens\":16}"
echo.
echo.
echo [提示] 若上面返回 JSON 含回复内容，说明服务正常。
pause
endlocal
