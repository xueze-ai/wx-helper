param(
    [ValidateRange(0, 86400)][int]$RunSeconds = 0,
    [string]$Python = $env:WXBOT_PYTHON
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'

if ([string]::IsNullOrWhiteSpace($Python)) {
    $taskVenvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $taskVenvPython) {
        $Python = $taskVenvPython
    } else {
        $taskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $taskPythonCommand) { throw '请安装 64 位 Python，或通过 -Python 指定解释器路径。' }
        $Python = $taskPythonCommand.Source
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bot_config.json'))) {
    & $Python -X utf8 (Join-Path $PSScriptRoot 'tools\init_config.py')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host '已生成本地配置。请填写自己的机器人账号、群聊和 API key，再次启动。'
    exit 0
}
& $Python -X utf8 (Join-Path $PSScriptRoot 'main.py') --run-seconds $RunSeconds
exit $LASTEXITCODE
