<#
.SYNOPSIS
    清理哨兵 Sentinel 留下的**开机自启残留**（v3.3.0）。

.DESCRIPTION
    哨兵 v3.1.0 ~ v3.2.1 提供过「开机自启」：
    在 Windows 的「启动」文件夹里放一个快捷方式，
    指向一个隐藏窗口的 VBS，开机就把哨兵**静默**拉起来。

    v3.3.0 起这个功能被**彻底删除** ——
    用户明确要求「拒绝任何形式的开机自启动和后台静默驻留」。

    但**已经装过的机器上还有残留**。
    本脚本负责把它们清干净：

    1. 「启动」文件夹里的「哨兵 Sentinel.lnk」快捷方式；
    2. 隐藏启动器 VBS（打包版的 ``dist\sentinel_startup.vbs``、
       源码版的 ``logs\sentinel_startup.vbs``）；
    3. 注册表 Run 键里**名字或值**带「哨兵 / sentinel」的条目
       （⚠️ 哨兵自己**从来不写**注册表，但用户可能手工加过）。

    删完之后，哨兵就**只在你手动启动时**才运行 ——
    「用完即走，想开才开」。

.PARAMETER Status
    只看不删（查残留用）。

.PARAMETER KeepRunKeys
    只删快捷方式与 VBS，**保留**注册表条目。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
      -File '_构建与发布\scripts\remove_startup_residue.ps1'

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
      -File '_构建与发布\scripts\remove_startup_residue.ps1' -Status
#>

[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$KeepRunKeys
)

$ErrorActionPreference = 'Stop'

$linkName = "哨兵 Sentinel.lnk"

# ⚠️ 脚本目录必须在**脚本作用域**取 ——
#    函数里的 ``$MyInvocation.MyCommand.Path`` 是**空的**
#    （那是 FunctionInfo 的路径，不是脚本的路径），
#    拿它去 Split-Path 会直接报
#    「Cannot bind argument to parameter 'Path' because it is null」。
$script:ScriptsDir = $PSScriptRoot

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 62)
    Write-Host ('  ' + $Title)
    Write-Host ('=' * 62)
}

function Get-StartupDir {
    # 返回「启动」文件夹路径。
    return [Environment]::GetFolderPath("Startup")
}

function Get-RepoRoot {
    # 脚本在 <repo>/_构建与发布/scripts/ 下 → 上两级就是仓库根。
    $buildDir = Split-Path -Parent $script:ScriptsDir
    return (Split-Path -Parent $buildDir)
}

function Get-RunKeyResidue {
    # 找注册表 Run 键里带哨兵字样的条目。
    $result = @()
    foreach ($root in @("HKCU:", "HKLM:")) {
        $key = "$root\Software\Microsoft\Windows\CurrentVersion\Run"
        if (-not (Test-Path $key)) { continue }
        try {
            $props = Get-ItemProperty -Path $key -ErrorAction Stop
        } catch {
            continue
        }
        foreach ($prop in $props.PSObject.Properties) {
            if ($prop.Name -like "PS*") { continue }
            $haystack = ("{0} {1}" -f $prop.Name, $prop.Value).ToLower()
            if ($haystack -match "哨兵" -or $haystack -match "sentinel") {
                $result += [pscustomobject]@{
                    Key   = $key
                    Name  = $prop.Name
                    Value = $prop.Value
                }
            }
        }
    }
    return $result
}

Write-Section '哨兵 Sentinel · 开机自启残留清理'

$repoRoot = Get-RepoRoot
$startup = Get-StartupDir
$link = Join-Path $startup $linkName
$vbsCandidates = @(
    (Join-Path $repoRoot "_构建与发布\dist\sentinel_startup.vbs"),
    (Join-Path $repoRoot "logs\sentinel_startup.vbs")
)
$runKeys = Get-RunKeyResidue

# ---- 先把「有什么」说清楚 -----------------------------------------------
$found = @()
if (Test-Path $link) {
    $found += "快捷方式   ：$link"
}
foreach ($vbs in $vbsCandidates) {
    if (Test-Path $vbs) {
        $found += "隐藏启动器 ：$vbs"
    }
}
foreach ($item in $runKeys) {
    $found += "注册表启动项：$($item.Key) → $($item.Name)"
}

if ($found.Count -eq 0) {
    Write-Host ''
    Write-Host '  [干净] 没找到任何自启残留 —— 这台机器上哨兵**不会**开机自启。' -ForegroundColor Green
    exit 0
}

Write-Host ''
Write-Host '  发现以下残留：'
foreach ($line in $found) {
    Write-Host ('    · ' + $line)
}

if ($Status) {
    Write-Host ''
    Write-Host '  （--Status：只看不删。要清干净请去掉这个开关再跑一次。）'
    exit 0
}

# ---- 开始清理 ----------------------------------------------------------
Write-Host ''
Write-Host '  正在清理……'

if (Test-Path $link) {
    try {
        Remove-Item -LiteralPath $link -Force -ErrorAction Stop
        Write-Host '    [已删] 快捷方式：' + $link -ForegroundColor Green
    } catch {
        Write-Host '    [失败] 删不掉快捷方式：' + $link -ForegroundColor Red
        Write-Host '           （可能有权限问题 —— 试试用管理员身份重跑）'
    }
} else {
    Write-Host '    [跳过] 快捷方式本来就不存在。'
}

foreach ($vbs in $vbsCandidates) {
    if (Test-Path $vbs) {
        try {
            Remove-Item -LiteralPath $vbs -Force -ErrorAction Stop
            Write-Host '    [已删] 隐藏启动器：' + $vbs -ForegroundColor Green
        } catch {
            Write-Host '    [失败] 删不掉隐藏启动器：' + $vbs -ForegroundColor Red
        }
    }
}

if ($KeepRunKeys) {
    Write-Host '    [跳过] 注册表启动项（--KeepRunKeys 指定保留）。'
} else {
    foreach ($item in $runKeys) {
        try {
            Remove-ItemProperty -Path $item.Key -Name $item.Name -Force -ErrorAction Stop
            Write-Host ('    [已删] 注册表启动项：' + $item.Key + ' → ' + $item.Name) -ForegroundColor Green
        } catch {
            Write-Host ('    [失败] 删不掉注册表项：' + $item.Name) -ForegroundColor Red
            Write-Host '           （HKLM 的项需要管理员权限）'
        }
    }
}

# ---- 收尾：把「现在哨兵怎么用」讲清楚 --------------------------------
Write-Host ''
Write-Host '  [完成] 残留清理完毕。' -ForegroundColor Green
Write-Host ''
Write-Host '  从现在起哨兵**只在你手动启动时**才会运行：'
Write-Host '    · 双击「启动哨兵.bat」→ 选「接管 Agent」或「演示模式」；'
Write-Host '    · 或者直接跑 sentinel.exe --help 看全部参数。'
Write-Host ''
Write-Host '  ⚠️ 用完它会**自己退出** —— 不会在后台悄悄待着。'
exit 0
