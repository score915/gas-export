param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('run', 'check', 'test', 'update', 'rebuild', 'restore', 'logs')]
    [string]$Task
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.local.json'
$privateDir = Join-Path $projectRoot '.gas-export'
$backupState = Join-Path $privateDir 'environment-backup.json'
$taskLock = $null
$transcribing = $false
$taskResult = 0

function Save-Json($Path, $Value) {
    $Value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-Python([string[]]$Arguments) {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw '仮想環境がありません。Python 3.11以上をインストールしてPATHに設定し、rebuild_env.batを実行してください。'
    }
    & $pythonPath @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Pythonの処理が失敗しました（終了コード $LASTEXITCODE）。上に表示された原因・対処を確認してください。"
    }
}

function Check-Setup {
    Invoke-Python @('run.py', 'check', '--config', 'config.local.json')
}

function Install-Packages([bool]$Upgrade) {
    Invoke-Python @('-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools>=77', 'wheel')
    $packageArgs = @('-m', 'pip', 'install', '-r', 'requirements.txt')
    if ($Upgrade) { $packageArgs += '--upgrade' }
    Invoke-Python $packageArgs
    Invoke-Python @('-m', 'pip', 'install', '-e', '.', '--no-deps')
    Invoke-Python @('-m', 'pip', 'check')
    Check-Setup
}

function Read-Config {
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw 'config.local.jsonがありません。README.ja.mdの設定手順を確認してください。'
    }
    return Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Resolve-ProjectPath([string]$Value) {
    if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Value))
}

function Check-BackupPath([string]$RelativePath) {
    # Only this task's private workspace directory can be a backup source.
    if (-not $RelativePath -or [IO.Path]::IsPathRooted($RelativePath)) {
        throw '環境バックアップのパスが不正です。'
    }
    $resolved = Resolve-ProjectPath $RelativePath
    $allowed = [IO.Path]::GetFullPath($privateDir) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) {
        throw '環境バックアップが作業フォルダの管理領域外を指しています。'
    }
    return $resolved
}

try {
    if ($Task -eq 'logs') {
        $cfg = Read-Config
        $logRoot = Resolve-ProjectPath $(if ($cfg.logs) { [string]$cfg.logs } else { 'logs' })
        $latest = Get-ChildItem -LiteralPath $logRoot -Recurse -Filter '*.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { throw '実行ログがありません。check_setup.batまたは取得バッチを実行してください。' }
        Write-Host "最新ログ: $($latest.FullName)"
        Get-Content -LiteralPath $latest.FullName -Encoding UTF8 -Tail 100
        exit 0
    }

    New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
    try {
        $taskLock = [IO.File]::Open((Join-Path $privateDir 'local-task.lock'),
            [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        throw '別のバッチが実行中です。完了してから再実行してください。'
    }
    $taskLogRoot = Join-Path $projectRoot 'logs'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $taskConfig = Read-Config
        if ($taskConfig.logs) { $taskLogRoot = Resolve-ProjectPath ([string]$taskConfig.logs) }
    }
    $logDir = Join-Path $taskLogRoot (Get-Date -Format yyyyMMdd)
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $taskLog = Join-Path $logDir ((Get-Date -Format HHmmss_fff) + '_batch_' + $Task + '.log')
    Start-Transcript -LiteralPath $taskLog | Out-Null
    $transcribing = $true
    Write-Host "作業: $Task / 設定: config.local.json"

    switch ($Task) {
        'check' {
            Invoke-Python @('--version')
            Invoke-Python @('-m', 'pip', '--version')
            Invoke-Python @('-m', 'pip', 'check')
            Check-Setup
        }
        'run' {
            Check-Setup
            Invoke-Python @('run.py', 'run', '--config', 'config.local.json')
        }
        'test' {
            Check-Setup
            $cfg = Read-Config
            $logRoot = Resolve-ProjectPath $(if ($cfg.logs) { [string]$cfg.logs } else { 'logs' })
            $ids = Get-ChildItem -LiteralPath $logRoot -Recurse -Filter 'verification-script-ids.json' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if (-not $ids) { throw '確認用ID一覧がありません。logs配下にverification-script-ids.jsonを用意してください。通常の全件取得はrun_export.batです。' }
            $entries = Get-Content -LiteralPath $ids.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($entries -isnot [array]) { throw '確認用ID一覧はJSON配列で指定してください。' }
            if ($entries.Count -eq 0 -or $entries.Count -gt 4) {
                throw '少数確認のID一覧は1〜4件にしてください。全件一覧を確認用として実行することはできません。'
            }
            Write-Host "確認用ID一覧: $($ids.FullName) / $($entries.Count)件"
            Invoke-Python @('run.py', 'export', '--config', 'config.local.json', '--ids', $ids.FullName,
                '--logs', (Join-Path $logRoot 'verification'))
        }
        'update' {
            Write-Host 'ライブラリを更新します。取得処理と取得用Chromeは終了しておいてください。'
            $freeze = & $pythonPath -m pip freeze
            if ($LASTEXITCODE -ne 0) { throw '更新前のライブラリ一覧を取得できません。check_setup.batで環境を確認してください。' }
            $packages = Join-Path $privateDir ('packages-' + (Get-Date -Format yyyyMMdd-HHmmss-fff) + '.txt')
            $freeze | Set-Content -LiteralPath $packages -Encoding UTF8
            Save-Json $backupState @{ kind = 'packages'; path = '.gas-export/' + [IO.Path]::GetFileName($packages) }
            Install-Packages $true
        }
        'rebuild' {
            # Verify the new interpreter BEFORE touching the existing .venv.
            $basePython = (Get-Command python -CommandType Application -ErrorAction Stop).Source
            & $basePython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
            if ($LASTEXITCODE -ne 0) {
                throw 'PATH上のpythonが3.11未満です。Python 3.11以上をインストールしてPATHに設定してください。既存の.venvは変更していません。'
            }
            & $basePython --version
            $venvPath = Join-Path $projectRoot '.venv'
            if (Test-Path -LiteralPath $venvPath) {
                $relativeBackup = '.gas-export/venv-' + (Get-Date -Format yyyyMMdd-HHmmss-fff)
                $venvBackup = Check-BackupPath $relativeBackup
                if (Test-Path -LiteralPath $venvBackup) { throw '環境バックアップ先が既に存在します。' }
                Save-Json $backupState @{ kind = 'venv'; path = $relativeBackup }
                Move-Item -LiteralPath $venvPath -Destination $venvBackup
            }
            & $basePython -m venv '.venv'
            if ($LASTEXITCODE -ne 0) { throw '仮想環境を作成できません。restore_env.batで以前の環境へ戻せます。' }
            Install-Packages $false
        }
        'restore' {
            if (-not (Test-Path -LiteralPath $backupState -PathType Leaf)) {
                throw 'このバッチが作成した環境バックアップがありません。'
            }
            $state = Get-Content -LiteralPath $backupState -Raw -Encoding UTF8 | ConvertFrom-Json
            $backup = Check-BackupPath ([string]$state.path)
            if ($state.kind -eq 'venv') {
                if (-not (Test-Path -LiteralPath $backup -PathType Container)) { throw '退避した仮想環境が見つかりません。' }
                $venvPath = Join-Path $projectRoot '.venv'
                if (Test-Path -LiteralPath $venvPath) {
                    $failed = Check-BackupPath ('.gas-export/venv-replaced-' + (Get-Date -Format yyyyMMdd-HHmmss-fff))
                    if (Test-Path -LiteralPath $failed) { throw '復元時の退避先が既に存在します。' }
                    Move-Item -LiteralPath $venvPath -Destination $failed
                }
                Move-Item -LiteralPath $backup -Destination $venvPath
                Save-Json $backupState @{ kind = 'restored'; path = [string]$state.path }
            } elseif ($state.kind -eq 'packages') {
                if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw '更新前のライブラリ一覧が見つかりません。' }
                Invoke-Python @('-m', 'pip', 'install', '-r', $backup)
                Invoke-Python @('-m', 'pip', 'check')
            } else {
                throw '仮想環境は復元済みです。check_setup.batで確認してください。'
            }
            Check-Setup
        }
    }
    Write-Host '完了しました。'
} catch {
    Write-Host ('エラー: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '上の内容を確認してください。詳細はlogs内のbatchログにあります。'
    $taskResult = 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($taskLock) { $taskLock.Dispose() }
}
exit $taskResult
