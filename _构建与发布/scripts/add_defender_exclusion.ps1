<#
.SYNOPSIS
    把哨兵的打包目录加入 Windows Defender 排除项（防误杀指南）。

.DESCRIPTION
    Microsoft Defender 的**机器学习**启发式有时会把单文件 exe 判成木马
    （v3.2.0 发布当夜就被隔离过一次，威胁名 Trojan:Win32/Bearfoos.A!ml）。

    后果不是「报个警」，而是**开机自启静默失效**：
    快捷方式还在，exe 没了，而且**不报错** ——
    用户只会以为哨兵「自己坏了」。

    这个脚本把打包目录加进 Defender 的排除路径，
    让 Defender 别再拿启发式去扫它。

    ⚠️ **需要管理员权限** ——
    Add-MpPreference 是系统级设置，普通用户改不了。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
      -File '_构建与发布\scripts\add_defender_exclusion.ps1'

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
      -File '_构建与发布\scripts\add_defender_exclusion.ps1' -Status
#>

[CmdletBinding()]
param(
    # 要排除的目录；留空 = 自动定位到本脚本上级的 dist 目录
    [string]$Path = "",
    # 只看状态，不改任何设置
    [switch]$Status,
    # 把排除项删掉
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 62)
    Write-Host ('  ' + $Title)
    Write-Host ('=' * 62)
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Resolve-TargetPath {
    param([string]$Given)
    if ($Given) {
        return (Resolve-Path -LiteralPath $Given).Path
    }
    # 脚本在 <repo>/_构建与发布/scripts/ 下 → 打包目录是上级的 dist
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $dist = Join-Path (Split-Path -Parent $here) 'dist'
    if (Test-Path -LiteralPath $dist) {
        return (Resolve-Path -LiteralPath $dist).Path
    }
    return $here
}

Write-Section '哨兵 · Windows Defender 防误杀助手'

if (-not (Test-Administrator)) {
    Write-Host '  [警告] 当前**不是管理员** —— 改不了 Defender 的系统设置。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  请用「管理员身份」重新打开 PowerShell，再执行一次本脚本：'
    Write-Host ''
    Write-Host '    1) 开始菜单搜「PowerShell」→ 右键 → 「以管理员身份运行」'
    Write-Host '    2) 复制下面这条命令执行：'
    Write-Host ''
    Write-Host ("       powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $MyInvocation.MyCommand.Path)
    Write-Host ''
    Write-Host '  或者自己敲这一条（把路径换成你的打包目录）：' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '       Add-MpPreference -ExclusionPath "<打包目录>"'
    Write-Host ''
    exit 2
}

$target = Resolve-TargetPath -Given $Path
Write-Host ('  打包目录：' + $target)

# Defender 本身可能被关了（或者换成了别的杀软）——
# 那 Get-MpPreference 会报错，得说人话而不是甩一屏红字。
$mp = Get-Command Get-MpPreference -ErrorAction SilentlyContinue
if (-not $mp) {
    Write-Host '  [提示] 这台机器上找不到 Defender 的管理命令。' -ForegroundColor Yellow
    Write-Host '  可能你用的是第三方杀软 —— 那就把它自己的「信任区」打开即可。'
    exit 3
}

try {
    $current = @(Get-MpPreference -ErrorAction Stop).ExclusionPath
} catch {
    Write-Host ('  [提示] 读取 Defender 设置失败：' + $_.Exception.Message) -ForegroundColor Yellow
    Write-Host '  可能 Defender 已被关闭或被组策略接管。'
    exit 3
}

$already = $current -contains $target

if ($Status) {
    Write-Section '当前排除项'
    if ($current.Count -eq 0) {
        Write-Host '  （一个都没有）'
    } else {
        foreach ($item in $current) {
            Write-Host ('  · ' + $item)
        }
    }
    if ($already) {
        Write-Host ''
        Write-Host '  [正常] 打包目录**已在**排除项里 —— 不会再被误杀。' -ForegroundColor Green
        exit 0
    }
    Write-Host ''
    Write-Host '  [注意] 打包目录**还不在**排除项里。' -ForegroundColor Yellow
    exit 1
}

if ($Remove) {
    if ($already) {
        Remove-MpPreference -ExclusionPath $target -ErrorAction Stop
        Write-Host ''
        Write-Host '  [完成] 已把打包目录从排除项里删掉。' -ForegroundColor Green
    } else {
        Write-Host ''
        Write-Host '  [跳过] 打包目录本来就不在排除项里。'
    }
    exit 0
}

if ($already) {
    Write-Host ''
    Write-Host '  [正常] 打包目录**已经**在排除项里，不用改。' -ForegroundColor Green
    exit 0
}

try {
    Add-MpPreference -ExclusionPath $target -ErrorAction Stop
} catch {
    Write-Host ''
    Write-Host ('  [失败] 写入排除项失败：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '  请确认：① 确实是管理员；② Defender 没有被组策略接管。'
    exit 4
}

Write-Host ''
Write-Host '  [完成] 已把打包目录加入 Defender 排除项。' -ForegroundColor Green
Write-Host '  开机自启不会再「静默失效」了。'
exit 0
