@echo off
call "D:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
set "PATH=E:\ai\ninfer-4090-native\build-ninja\apps;%PATH%"
cd /d E:\ai\ninfer-4090-native
set W=E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer
set C=bench\fixtures\bench_corpus.ids

echo ##### GROUP 1: Prefill INT8 (README pp512/pp2048/pp4096) #####
build-ninja\bench\ninfer_bench.exe --weights "%W%" --corpus %C% --kv-dtype int8 --mtp-draft-tokens 0 -p 512,2048,4096 -n 0 -r 3 --warmup 1

echo ##### GROUP 2: Decode MTP0 baseline INT8 (README 52.8) #####
build-ninja\bench\ninfer_bench.exe --weights "%W%" --corpus %C% --kv-dtype int8 --mtp-draft-tokens 0 -pg 2048,128 -r 3 --warmup 1

echo ##### GROUP 3: Decode Prompt-Cached MTP7 INT8 (README 218.3) #####
build-ninja\bench\ninfer_bench.exe --weights "%W%" --corpus %C% --kv-dtype int8 --mtp-draft-tokens 7 --lm-head-draft -pg 2048,128 -r 3 --warmup 1

echo ##### GROUP 4: Decode Prompt-Cached MTP7 rk4v4-e8 (README 216.9) #####
build-ninja\bench\ninfer_bench.exe --weights "%W%" --corpus %C% --kv-dtype rk4v4-e8 --mtp-draft-tokens 7 --lm-head-draft -pg 2048,128 -r 3 --warmup 1

echo ##### GROUP 5: Decode Deep Context MTP7 rk4v4-e8 (README 229.9) #####
build-ninja\bench\ninfer_bench.exe --weights "%W%" --corpus %C% --kv-dtype rk4v4-e8 --mtp-draft-tokens 7 --lm-head-draft -pg 32768,128 --max-ctx 40000 -r 3 --warmup 1

echo ##### GROUP 6: Decode Cold Corpus MTP4 rk4v4-e8 (README 89.2) #####
build-ninja\bench\ninfer_bench.exe --weights "%W%" --corpus %C% --kv-dtype rk4v4-e8 --mtp-draft-tokens 4 --lm-head-draft -pg 512,128 -r 3 --warmup 0

echo ALL_DONE
