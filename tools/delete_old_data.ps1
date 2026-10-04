param(
    [ValidateRange(1, 36500)]
    [int]$KeepDays = 30,
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskLock = $null
$transcribing = $false
$taskResult = 0

function Assert-WorkspacePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $prefix = $projectRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '削除対象は作業フォルダ内の専用フォルダにしてください。作業フォルダ自体や外部のパスは削除できません。'
    }
    # Resolve every existing ancestor before any recursive removal.
    $cursor = $full
    while ($cursor -and $cursor -ne $projectRoot) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "リンク・ジャンクションを含むパスは処理できません: $cursor"
            }
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Resolve-DataRoot($Value, [string]$Default) {
    if ($null -eq $Value) { $Value = $Default }
    if ($Value -isnot [string] -or -not $Value.Trim()) { throw 'out / logsは空でないパスで指定してください。' }
    $path = if ([IO.Path]::IsPathRooted($Value)) { $Value } else { Join-Path $projectRoot $Value }
    return Assert-WorkspacePath $path
}

function Assert-NoLinks([string]$Path) {
    $null = Assert-WorkspacePath $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "対象フォルダがありません: $Path" }
    foreach ($item in Get-ChildItem -LiteralPath $Path -Force -Recurse) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "削除対象内にリンク・ジャンクションがあります。削除を中止します: $($item.FullName)"
        }
    }
}

function Find-OldDirectories([string]$Root, [datetime]$Cutoff) {
    if (-not (Test-Path -LiteralPath $Root)) { return }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "保存先がフォルダではありません: $Root" }
    $dated = @(foreach ($item in Get-ChildItem -LiteralPath $Root -Directory -Force) {
        $date = [datetime]::MinValue
        if ($item.Name -match '^\d{8}$' -and [datetime]::TryParseExact(
            $item.Name, 'yyyyMMdd', [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None, [ref]$date)) {
            [PSCustomObject]@{ Path = $item.FullName; Date = $date }
        }
    })
    $latest = $dated | Sort-Object Date -Descending | Select-Object -First 1
    foreach ($entry in $dated) {
        if ($entry.Date -lt $Cutoff -and $entry.Path -ne $latest.Path) { $entry.Path }
    }
}

try {
    $config = Get-Content -LiteralPath (Join-Path $projectRoot 'config.local.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $outRoot = Resolve-DataRoot $config.out 'output'
    $logRoot = Resolve-DataRoot $config.logs 'logs'
    $separator = [IO.Path]::DirectorySeparatorChar
    if ($outRoot -eq $logRoot -or $outRoot.StartsWith($logRoot + $separator, [StringComparison]::OrdinalIgnoreCase) -or
        $logRoot.StartsWith($outRoot + $separator, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'outとlogsは互いに含まれない別のフォルダを指定してください。'
    }
    $protected = @('.git', '.gas-export', '.venv', 'src', 'tools', 'tests', 'build', 'dist', 'browser-profile',
        'config.local.json', 'token.drive.readonly.json', 'token.script.json')
    if ($config.browserProfile) { $protected += [string]$config.browserProfile }
    foreach ($key in @('credentials', 'driveToken', 'scriptToken', 'browserState')) {
        if ($config.$key) { $protected += [string]$config.$key }
    }
    foreach ($name in $protected) {
        $path = if ([IO.Path]::IsPathRooted($name)) { [IO.Path]::GetFullPath($name) } else { [IO.Path]::GetFullPath((Join-Path $projectRoot $name)) }
        foreach ($root in @($outRoot, $logRoot)) {
            if ($root -eq $path -or $root.StartsWith($path + $separator, [StringComparison]::OrdinalIgnoreCase) -or
                $path.StartsWith($root + $separator, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'out / logsがソース・環境・認証の管理フォルダと重なっています。専用の保存先を指定してください。'
            }
        }
    }
    $private = Assert-WorkspacePath (Join-Path $projectRoot '.gas-export')
    New-Item -ItemType Directory -Path $private -Force | Out-Null
    try {
        $taskLock = [IO.File]::Open((Join-Path $private 'local-task.lock'),
            [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch { throw '別のバッチが実行中です。完了してから再実行してください。' }

    $cutoff = (Get-Date).Date.AddDays(-($KeepDays - 1))
    $roots = @($outRoot, $logRoot)
    $verification = Join-Path $logRoot 'verification'
    if (Test-Path -LiteralPath $verification) { $roots += (Assert-WorkspacePath $verification) }
    $targets = @(foreach ($root in $roots) { Find-OldDirectories $root $cutoff })
    # Validate ALL targets before offering or performing deletion.
    foreach ($target in $targets) { Assert-NoLinks $target }
    $todayLog = Assert-WorkspacePath (Join-Path $logRoot (Get-Date -Format yyyyMMdd))
    New-Item -ItemType Directory -Path $todayLog -Force | Out-Null
    $logPath = Join-Path $todayLog ((Get-Date -Format HHmmss_fff) + '_batch_cleanup.log')
    Start-Transcript -LiteralPath $logPath | Out-Null
    $transcribing = $true
    Write-Host "保持: 今日を含む直近 $KeepDays 日と、各保存先の最新の日付フォルダ"
    Write-Host "削除するのは $($cutoff.ToString('yyyyMMdd')) より前の日付フォルダです。"
    Write-Host "削除候補: $($targets.Count) フォルダ"
    foreach ($target in $targets) { Write-Host "  $target" }
    if ($Preview) {
        Write-Host '確認のみです。削除していません。'
    } elseif ($targets.Count -eq 0) {
        Write-Host '削除対象はありません。'
    } else {
        Write-Host '表示したフォルダとその中のファイルを完全に削除します。ごみ箱には入りません。'
        $answer = Read-Host '削除する場合は Y を入力してEnter。それ以外は中止'
        if ($answer -ieq 'Y') {
            foreach ($target in $targets) {
                # Revalidate absolute paths and links immediately before removal.
                Assert-NoLinks $target
                Remove-Item -LiteralPath $target -Recurse -Force
                Write-Host "削除しました: $target"
            }
            Write-Host '整理が完了しました。'
        } else { Write-Host '中止しました。削除していません。' }
    }
    Write-Host "結果ログ: $logPath"
} catch {
    Write-Host ('エラー: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'config.local.jsonのout / logsと保存先を確認してください。'
    $taskResult = 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($taskLock) { $taskLock.Dispose() }
}
exit $taskResult
