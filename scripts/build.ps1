# AutoFarmStation 打包脚本
#
# 注意:此文件必须保存为 UTF-8 with BOM,否则 PowerShell 5.1 会把脚本中的
# 字符串读成乱码并解析报错。
#
# 用法(在仓库根目录):
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#
# 产物:dist\AutoFarmStation.exe
# 可选参数:
#   -SkipClean  跳过 clean(增量打包,加快二次构建)

[CmdletBinding()]
param(
    [switch]$SkipClean = $false,
    [string]$Spec = "AutoFarmStation.spec"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# === 路径 ===
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $RepoRoot
Write-Host "[build] RepoRoot: $RepoRoot" -ForegroundColor Cyan

# === Step 0: 自检 ===
Write-Host "[build] 运行自检..." -ForegroundColor Cyan
# logger 写 stderr → 默认 ErrorActionPreference=Stop 会把脚本中断,
# 临时改 Continue 跑完 selftest,只以退出码判断
$ErrorActionPreference = "Continue"
& python scripts\selftest.py 2>&1 | Out-Null
$selftestExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($selftestExit -ne 0) {
    Write-Host "[build] 自检失败,中止打包" -ForegroundColor Red
    exit 1
}

# === Step 1: 清理 ===
if (-not $SkipClean) {
    Write-Host "[build] 清理 build/dist..." -ForegroundColor Cyan
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    if (Test-Path dist) { Remove-Item -Recurse -Force dist }
}

# === Step 1.5: 生成 exe 版本资源 ===
Write-Host "[build] 生成版本资源文件..." -ForegroundColor Cyan
& python scripts\build_exe_version.py --out build\version_info.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[build] 版本资源生成失败,中止打包" -ForegroundColor Red
    exit 1
}

# === Step 2: PyInstaller ===
Write-Host "[build] PyInstaller 打包(单文件,可能 3-5 分钟)..." -ForegroundColor Cyan

# 用 --name 显式指定 exe 名(覆盖 spec 中的 name)
# 用 --noconfirm 避免提示
# 用 --collect-all mss / pynput 把隐藏子模块打包进(避免漏资源)
# 用 --version-file 写入 Windows 版本资源(FileVersion = 当前版本号)
$pyiArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "AutoFarmStation",
    "--version-file", "build\version_info.txt",
    "--collect-all", "mss",
    "--collect-all", "pynput",
    "--collect-all", "pycaw",
    "--collect-all", "comtypes",
    "--collect-submodules", "pynput.keyboard",
    "--collect-submodules", "pynput.mouse",
    "--hidden-import", "PySide6.QtCore",
    "--hidden-import", "PySide6.QtGui",
    "--hidden-import", "PySide6.QtWidgets",
    "--add-data", "bat;bat",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "pytest",
    "--paths", ".",
    "main.py"
)
$ErrorActionPreference = "Continue"
& python -m PyInstaller @pyiArgs 2>&1 | Out-Null
$pyiExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pyiExit -ne 0) {
    Write-Host "[build] PyInstaller 失败" -ForegroundColor Red
    exit 1
}

# === Step 3: 校验 ===
$exe = Join-Path $RepoRoot "dist\AutoFarmStation.exe"
if (-not (Test-Path $exe)) {
    Write-Host "[build] 找不到产物 $exe" -ForegroundColor Red
    exit 1
}
$size = (Get-Item $exe).Length / 1MB
Write-Host ("[build] OK 产物: {0} ({1:N1} MB)" -f $exe, $size) -ForegroundColor Green

# === Step 4: 版本号写入校验 ===
Write-Host "[build] 校验 exe 版本资源..." -ForegroundColor Cyan
$ver = (& python -c "from autofarmstation import __version__; print(__version__)").Trim()
$info = (Get-Item $exe).VersionInfo
Write-Host ("[build] FileVersion={0}  ProductVersion={1}" -f $info.FileVersion, $info.ProductVersion) -ForegroundColor Yellow
if (-not $info.FileVersion) {
    Write-Host "[build] !! 失败: exe 未写入版本资源(检查 build\version_info.txt)" -ForegroundColor Red
    exit 1
}
if ($info.FileVersion -notlike "$ver*") {
    Write-Host ("[build] !! 失败: exe 版本 {0} 与源码版本 {1} 不一致" -f $info.FileVersion, $ver) -ForegroundColor Red
    exit 1
}
Write-Host "[build] OK 版本资源校验通过: $ver" -ForegroundColor Green

# === Step 5: 生成 SHA-256 sidecar(自动更新的完整性校验依赖它,#3) ===
# 发布时必须把这两个文件一起传到同一个 release:
#   AutoFarmStation.exe            → 资产本体
#   AutoFarmStation.exe.sha256     → 纯文本,内含 64 位十六进制摘要
# 软件端(utils/updater.py)只认本仓库 releases/download 前缀的下载地址,
# 并且拿不到校验值就**拒绝自动更新** —— 少了这个 sidecar,用户只能手动下载。
$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLower()
$sidecar = "$exe.sha256"
Set-Content -LiteralPath $sidecar -Value ("{0}  AutoFarmStation.exe" -f $hash) -Encoding ascii -NoNewline
Write-Host ("[build] OK SHA-256: {0}" -f $hash) -ForegroundColor Green
Write-Host ("[build] OK 校验文件: {0}(发布时与 exe 一起上传到同一个 release)" -f $sidecar) -ForegroundColor Green

# === Step 6: 代码签名(可选,但强烈建议) ===
# 未签名的 exe 会被 SmartScreen / 杀软拦,也无法在更新链路上做 Authenticode 校验。
# 有证书时设置环境变量后重跑本脚本即可自动签名:
#   $env:AFS_SIGN_CERT   = "CN=Your Name"      (证书存储里的主题名),或
#   $env:AFS_SIGN_PFX    = "C:\path\to\cert.pfx"
#   $env:AFS_SIGN_PASS   = "pfx 密码"
if ($env:AFS_SIGN_PFX -or $env:AFS_SIGN_CERT) {
    $signer = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signer) {
        Write-Host "[build] !! 找不到 signtool.exe(Windows SDK),跳过签名" -ForegroundColor Yellow
    } else {
        $authArgs = if ($env:AFS_SIGN_PFX) {
            @("/f", $env:AFS_SIGN_PFX, "/p", $env:AFS_SIGN_PASS)
        } else {
            @("/n", $env:AFS_SIGN_CERT)
        }
        & signtool.exe sign /fd SHA256 /tr "http://timestamp.digicert.com" /td SHA256 @authArgs $exe
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[build] !! 签名失败" -ForegroundColor Red
            exit 1
        }
        # 签名会改变文件内容 → sidecar 必须重算
        $hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLower()
        Set-Content -LiteralPath $sidecar -Value ("{0}  AutoFarmStation.exe" -f $hash) -Encoding ascii -NoNewline
        Write-Host ("[build] OK 已签名,SHA-256 已更新: {0}" -f $hash) -ForegroundColor Green
    }
} else {
    Write-Host "[build] 未配置签名(AFS_SIGN_PFX / AFS_SIGN_CERT 都没设),产物保持未签名" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[build] OK 完成。可执行: $exe" -ForegroundColor Green