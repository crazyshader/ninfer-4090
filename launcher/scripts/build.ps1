# ----------------------------------------------------------------
# ninfer-launcher 构建打包脚本（PyInstaller onedir，GUI + CLI）。
#
# 流程：环境检查 → 单元测试 → 清理 → GUI 构建 → CLI 构建 → 拷贝内置预设
#       → 产物校验（含 CLI status 冒烟）→ （可选）GUI 冒烟启动。
#
# 产物：
#   dist\ninfer-launcher\ninfer-launcher.exe        （GUI，onedir）
#   dist\ninfer-launcher-cli\ninfer-launcher-cli.exe（CLI，onedir，console=True）
# ----------------------------------------------------------------
param(
    [switch]$SkipTests,
    [switch]$Clean,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$root        = Split-Path -Parent $PSScriptRoot
$distDir     = Join-Path $root "dist"
$appDir      = Join-Path $distDir "ninfer-launcher"
$exePath     = Join-Path $appDir "ninfer-launcher.exe"
$specFile    = Join-Path $root "ninfer-launcher.spec"
$cliAppDir   = Join-Path $distDir "ninfer-launcher-cli"
$cliExePath  = Join-Path $cliAppDir "ninfer-launcher-cli.exe"
$cliSpecFile = Join-Path $root "ninfer-launcher-cli.spec"
$startedAt   = Get-Date

function Write-Step([int]$n, [string]$msg) {
    Write-Host ""
    Write-Host "[$n/8] $msg" -ForegroundColor Cyan
}

# ---------------------------------------------------------------- 1. 环境检查
Write-Step 1 "环境检查（Python / PySide6 / pynvml / PyInstaller）"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "未找到 python，请先安装 Python 3.10+" }
$pyVer = & $py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "  python    : $pyVer ($py)"

$deps = @()
foreach ($mod in @("PySide6", "pynvml", "PyInstaller")) {
    $line = & $py -c "import $mod; print(getattr($mod, '__version__', 'ok'))" 2>&1
    if ($LASTEXITCODE -ne 0) { $deps += $mod }
    else { Write-Host "  $mod : $line" }
}
if ($deps.Count -gt 0) {
    Write-Warning "缺少依赖：$($deps -join ', ')。正在安装 requirements.txt + pyinstaller ..."
    & $py -m pip install --quiet -r (Join-Path $root "requirements.txt") pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
    Write-Host "  依赖安装完成" -ForegroundColor Green
}

# ---------------------------------------------------------------- 2. 单元测试
if ($SkipTests) {
    Write-Step 2 "单元测试（已跳过）"
} else {
    Write-Step 2 "运行单元测试"
    Push-Location $root
    try { & $py -m pytest tests -q } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "单元测试未通过，中止构建" }
    Write-Host "  测试全部通过" -ForegroundColor Green
}

# ---------------------------------------------------------------- 3. 清理
# 删除旧产物目录；删不掉（运行中的进程仍持有其中文件——典型情形：ninfer-serve
# 由旧版 GUI 拉起后仍映射着 dist 里的 VCRUNTIME*.dll；NTFS 上镜像映射中的文件
# 无法删除、但允许重命名）时移到 .stale-<时间戳> 目录让构建继续。运行中的服务
# 不受影响（句柄按文件 ID 跟踪）；该目录可待服务停止后手动删除。
function Remove-OrStash([string]$path) {
    if (-not (Test-Path $path)) { return }
    try {
        Remove-Item $path -Recurse -Force -ErrorAction Stop
        Write-Host "  已删除：$path"
    } catch {
        $alt = "$path.stale-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
        try {
            Rename-Item -LiteralPath $path -NewName (Split-Path $alt -Leaf) -ErrorAction Stop
            Write-Warning "$path 无法删除（有运行中进程仍持有其中文件），已移至 $alt；待相关服务停止后可手动删除"
        } catch {
            throw "$path 无法删除也无法改名：请手动结束占用它的进程（ninfer-launcher* / ninfer-serve）后重试"
        }
    }
}

Write-Step 3 "清理旧产物（含解除产物目录占用）"
# 上次构建后仍开着的 GUI/CLI 实例会锁住 dist 里的 .pyd，导致 PyInstaller 清理时
# PermissionError: [WinError 5]。这里只结束「可执行文件位于本 dist 目录」的本工具
# 进程；ninfer-serve（推理服务）与其他进程一律不受影响。
$stale = Get-Process -Name "ninfer-launcher", "ninfer-launcher-cli" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($distDir, [System.StringComparison]::OrdinalIgnoreCase) }
if ($stale) {
    foreach ($p in $stale) {
        Write-Host "  终止残留实例：$($p.ProcessName) (PID $($p.Id))" -ForegroundColor Yellow
        $p | Stop-Process -Force
    }
    Start-Sleep -Seconds 2
    $still = Get-Process -Name "ninfer-launcher", "ninfer-launcher-cli" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($distDir, [System.StringComparison]::OrdinalIgnoreCase) }
    if ($still) {
        throw "仍有实例占用产物目录，构建无法进行：$($still.Path -join '; ')。请手动关闭后重试。"
    }
}
if ($Clean) {
    foreach ($d in @((Join-Path $root "build"), $distDir)) { Remove-OrStash $d }
} else {
    # 增量构建：预清两个产物目录。PyInstaller 的 COLLECT 阶段对「删不掉的目录」
    # 直接报 PermissionError，所以在这里先行处理，报错信息也更可读。
    # 本工具自身的残留实例已在上方结束；若仍删不掉，说明是 ninfer-serve 等外部
    # 进程持有旧 dll（由旧版 GUI 拉起的服务仍映射着旧 dist 的运行时文件），走改名。
    foreach ($d in @($appDir, $cliAppDir)) { Remove-OrStash $d }
    Write-Host "  （-Clean 可连同 build/ 工作目录与整个 dist 一起清理）"
}

# ------------------------------------------------------- 4. GUI PyInstaller 构建
Write-Step 4 "PyInstaller 构建 GUI（onedir，console=False）"
& $py -m PyInstaller $specFile --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller GUI 构建失败" }

# -------------------------------------------------------- 5. CLI PyInstaller 构建
Write-Step 5 "PyInstaller 构建 CLI（onedir，console=True，excludes PySide6）"
& $py -m PyInstaller $cliSpecFile --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller CLI 构建失败" }

# ------------------------------------------------------- 6. 拷贝内置预设
Write-Step 6 "拷贝内置预设到产物目录"
$resSrc = Join-Path $root "resources"
$resDst = Join-Path $appDir "resources"
if (-not (Test-Path $resSrc)) { throw "缺少 resources 目录：$resSrc" }
# PyInstaller 6 的 datas 落在 _internal 下，这里再在 exe 旁根目录放一份，
# 保证 config.builtin_presets_dir() 的第一候选命中，行为与开发模式一致
Copy-Item $resSrc $appDir -Recurse -Force
$presetsInDist = @(Get-ChildItem (Join-Path $resDst "presets") -Filter *.json -ErrorAction SilentlyContinue)
if ($presetsInDist.Count -eq 0) { throw "产物中未找到内置预设" }
Write-Host "  内置预设：$($presetsInDist.Name -join ', ')"
# 说明：CLI 故意不随包分发内置预设（docs 第 9 节）——配置根没有预设时明确报
# no-preset，而不是悄悄用出厂预设起一个用户没配过的服务。

# ---------------------------------------------------------------- 7. 校验产物
Write-Step 7 "校验产物（含 CLI status 冒烟）"
if (-not (Test-Path $exePath)) { throw "未找到产物：$exePath" }
$exeInfo = Get-Item $exePath
$sizeMb  = [math]::Round($exeInfo.Length / 1MB, 1)
$distMb  = [math]::Round(((Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "  gui exe  : $exePath ($sizeMb MB)"
Write-Host "  gui 大小 : $distMb MB（$appDir）"

if (-not (Test-Path $cliExePath)) { throw "未找到产物：$cliExePath" }
$cliInfo = Get-Item $cliExePath
$cliSizeMb = [math]::Round($cliInfo.Length / 1MB, 1)
$cliDistMb = [math]::Round(((Get-ChildItem $cliAppDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "  cli exe  : $cliExePath ($cliSizeMb MB)"
Write-Host "  cli 大小 : $cliDistMb MB（$cliAppDir）"

# CLI 冒烟：跑 "status"，断言退出码 0 且 stdout 是单行合法 JSON（键齐全）。
# 这是唯一能证明「输出契约没被打破」的自动检查——比检查 exe 存在有意义得多。
# 注意 stdout 按 UTF-8 解码（CLI 强制 UTF-8 输出），不能走 PowerShell 的默认代码页。
$smoke = & $py -c "import json, subprocess; p = subprocess.run([r'$cliExePath', 'status'], capture_output=True, timeout=120); data = json.loads(p.stdout.decode('utf-8')); print('rc=%d ok=%s state=%s keys=%d' % (p.returncode, data.get('ok'), data.get('state'), len(data)))"
if ($LASTEXITCODE -ne 0) { throw "CLI 冒烟失败：$smoke" }
$smokeLine = "$smoke"
Write-Host "  cli 冒烟 : $smokeLine"

# ---------------------------------------------------------------- 8. 冒烟启动
if ($Launch) {
    Write-Step 8 "冒烟测试（GUI 启动 5 秒）"
    $proc = Start-Process -FilePath $exePath -WorkingDirectory $appDir -PassThru
    Start-Sleep -Seconds 5
    if ($proc.HasExited) {
        throw "冒烟失败：进程提前退出（exit code $($proc.ExitCode)）"
    }
    Write-Host "  进程存活（PID $($proc.Id)），终止" -ForegroundColor Green
    $proc.Kill()
    $proc.WaitForExit()
} else {
    Write-Step 8 "冒烟测试（已跳过，使用 -Launch 启用）"
}

# ---------------------------------------------------------------- 汇总
$elapsed = (Get-Date) - $startedAt
Write-Host ""
Write-Host "=== 构建完成：$appDir + $cliAppDir ===" -ForegroundColor Green
Write-Host ("总耗时：{0:N0} 秒" -f $elapsed.TotalSeconds)
Write-Host '分发方式：整目录拷贝 dist
infer-launcher（GUI）与 dist
infer-launcher-cli（CLI）'
