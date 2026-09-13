# vcpkg 安装脚本（绕过 versioning checkout 的 rename 失败）
#
# 问题：vcpkg 把 port 的 git tree 检出到
#   buildtrees\versioning_\versions\<port>\<tree>_<pid>.tmp
# 再 rename 成 <tree>，这一步在本机稳定失败：
#   rename_or_delete(...): The process cannot access the file because it is being used by another process
# 重试 6 次全部卡在同一个 port，不是瞬时锁。
#
# 绕过：vcpkg 发现目标目录 <tree> 已存在时会跳过 checkout。
# 所以失败后用 git archive 把同一个 tree 直接解包到目标目录，再重跑。
# 内容来自同一个 tree hash，与 vcpkg 自己检出的结果一致。

$ErrorActionPreference = 'Continue'
$vcpkgExe    = 'E:\vcpkg\vcpkg.exe'
$vcpkgRoot   = 'E:\vcpkg'
$manifestDir = 'E:\ai\ninfer-4090-native'
$maxRounds   = 60

for ($round = 1; $round -le $maxRounds; $round++) {
    Write-Host "===== round $round / $maxRounds =====" -ForegroundColor Cyan

    $output = & $vcpkgExe install --x-manifest-root=$manifestDir 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        $line
    }
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        Write-Host "===== SUCCESS on round $round =====" -ForegroundColor Green
        exit 0
    }

    # 只处理 rename_or_delete 这一类失败，其它错误直接抛给使用者
    $text  = $output -join "`n"
    $match = [regex]::Match($text, 'rename_or_delete\("([^"]+)",\s*"([^"]+)"\)')
    if (-not $match.Success) {
        Write-Host "===== 非 rename 失败，停止（见上方输出）=====" -ForegroundColor Red
        exit $code
    }

    $tmpPath = $match.Groups[1].Value -replace '/', '\'
    $dstPath = $match.Groups[2].Value -replace '/', '\'
    $tree    = Split-Path $dstPath -Leaf

    if ($tree -notmatch '^[0-9a-f]{40}$') {
        Write-Host "目标目录名不是 git tree hash：$tree，停止" -ForegroundColor Red
        exit $code
    }

    Write-Host "绕过：git archive $tree -> $dstPath" -ForegroundColor Yellow

    # 清掉 vcpkg 留下的临时目录
    if (Test-Path $tmpPath) {
        Remove-Item $tmpPath -Recurse -Force -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Force -Path $dstPath | Out-Null

    # 经管道传 tar 会破坏二进制流，必须落中间文件
    $tarFile = Join-Path $env:TEMP "vcpkg_$tree.tar"
    Push-Location $vcpkgRoot
    & git archive --format=tar -o $tarFile $tree
    $gitCode = $LASTEXITCODE
    Pop-Location

    if ($gitCode -ne 0) {
        Write-Host "git archive 失败（tree=$tree），停止" -ForegroundColor Red
        exit 1
    }

    & tar -x -f $tarFile -C $dstPath
    $tarCode = $LASTEXITCODE
    Remove-Item $tarFile -Force -ErrorAction SilentlyContinue

    if ($tarCode -ne 0) {
        Write-Host "tar 解包失败（tree=$tree），停止" -ForegroundColor Red
        exit 1
    }

    $n = (Get-ChildItem $dstPath -Force | Measure-Object).Count
    Write-Host "已就位：$dstPath（$n 个条目）" -ForegroundColor Green
}

Write-Host "===== 超过 $maxRounds 轮仍未完成 =====" -ForegroundColor Red
exit 1
