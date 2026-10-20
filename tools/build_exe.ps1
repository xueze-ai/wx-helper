# 将图形控制台打包为 Windows x64 程序。
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1 -NoInstall
#   powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1 -OneFile
param(
    [string]$Python = "",
    [switch]$NoInstall,
    [switch]$OneFile
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Find-Python {
    if ($script:Python) { return $script:Python }
    $venv = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $local = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

$Python = Find-Python
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
    throw '未找到可用的 Python。请安装 64 位 Python 3.12，或通过 -Python 指定解释器路径。'
}
$bits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($bits -ne '64') { throw "需要 64 位 Python，当前解释器为 $bits 位：$Python" }

if (-not $NoInstall) {
    Write-Host "安装运行与构建依赖：$Python"
    & $Python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '运行依赖安装失败' }
    & $Python -m pip install --disable-pip-version-check -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw '构建依赖安装失败' }
}

$work = Join-Path $root 'build/pyinstaller'
$distRoot = Join-Path $root 'dist'
$mode = if ($OneFile) { '--onefile' } else { '--onedir' }

Write-Host '开始 PyInstaller 构建…'
& $Python -m PyInstaller --noconfirm --clean --windowed $mode `
    --name wx-helper `
    --distpath $distRoot `
    --workpath $work `
    --specpath $work `
    --hidden-import pefile `
    --hidden-import ctypes.wintypes `
    --collect-submodules wxbot_framework `
    (Join-Path $root 'gui.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }

$appDir = if ($OneFile) { $distRoot } else { Join-Path $distRoot 'wx-helper' }
$exe = Join-Path $appDir 'wx-helper.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "未生成 $exe" }

# 插件是可编辑的外部文件，必须放在 EXE 同级目录，不能打进单文件包。
$pluginDest = Join-Path $appDir 'plugins'
if (Test-Path -LiteralPath $pluginDest) { Remove-Item -LiteralPath $pluginDest -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $root 'plugins') -Destination $pluginDest -Recurse
Get-ChildItem -LiteralPath $pluginDest -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $pluginDest -Recurse -File |
    Where-Object { $_.Extension -in '.pyc', '.pyo' } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 原生桥接：只复制运行时需要的 Python 服务、版本指纹和两个 DLL。
$nativeDest = Join-Path $appDir 'native'
if (Test-Path -LiteralPath $nativeDest) { Remove-Item -LiteralPath $nativeDest -Recurse -Force }
New-Item -ItemType Directory -Path $nativeDest -Force | Out-Null
foreach ($name in @('filehelper_service.py', 'text_service.py', 'run_selftest.py', 'compatibility.json')) {
    Copy-Item -LiteralPath (Join-Path $root "native/$name") -Destination $nativeDest
}
New-Item -ItemType Directory -Path (Join-Path $nativeDest 'build') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $nativeDest 'build_text') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'native/build/selftest_bridge.dll') `
    -Destination (Join-Path $nativeDest 'build')
Copy-Item -LiteralPath (Join-Path $root 'native/build_text/text_bridge.dll') `
    -Destination (Join-Path $nativeDest 'build_text')

# 首次运行由界面从 example 生成本地配置；不复制真实账号和密钥。
foreach ($name in @('bot_config.example.json', 'members_config.example.json', 'llm_config.example.json',
                    'README.md', 'LICENSE', 'SECURITY.md', 'PRIVACY.md', 'CONTRIBUTING.md',
                    'CODE_OF_CONDUCT.md', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $appDir
}

# 保留 README 中的界面截图，保证离线阅读时文档链接可用
$docsDest = Join-Path $appDir 'docs'
if (Test-Path -LiteralPath $docsDest) { Remove-Item -LiteralPath $docsDest -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $root 'docs') -Destination $appDir -Recurse

$sizeMb = [math]::Round((Get-ChildItem -LiteralPath $appDir -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host ''
Write-Host "构建完成：$exe"
Write-Host "输出目录：$appDir（约 $sizeMb MB）"
Write-Host '把整个目录复制到目标电脑；首次运行会自动生成 bot_config.json、llm_config.json、members_config.json。'
