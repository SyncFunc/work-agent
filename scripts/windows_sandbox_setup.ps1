# Windows Agent 硬沙箱初始化脚本（管理员权限运行一次）
#
# 对齐 Codex elevated sandbox：
#   - 创建本地用户 CodexSandboxOffline / CodexSandboxOnline（CodexSandboxUsers 组）
#   - 创建合成 SID sandbox-write（持久化到 <workspace>/.agent/sandbox-write.sid）
#   - DPAPI 加密保存沙箱凭据（<workspace>/.agent/sandbox_creds.bin）
#   - 防火墙规则按 CodexSandboxOffline 的 SID 绑定，禁止该用户所有出站
#   - workspace / writable_roots 授予 sandbox-write SID Write/Execute/Delete
#   - .git / .codex / .agents / .agent 对 sandbox-write SID 显式 Deny Write
#
# 用法：以管理员打开 PowerShell，运行：
#   powershell -ExecutionPolicy Bypass -File scripts/windows_sandbox_setup.ps1

param(
    [string]$GroupName = "CodexSandboxUsers",
    [string]$Password = "",
    [string]$Workspace = "",
    [string[]]$WritableRoots = @(),
    [string[]]$ReadDirs = @()
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-SandboxWriteSid {
    $r1 = Get-Random -Minimum 100000000 -Maximum 2147483647
    $r2 = Get-Random -Minimum 100000000 -Maximum 2147483647
    $r3 = Get-Random -Minimum 100000000 -Maximum 2147483647
    $r4 = Get-Random -Minimum 100000000 -Maximum 2147483647
    return "S-1-5-21-$r1-$r2-$r3-$r4"
}

if (-not (Test-Admin)) {
    Write-Error "需要以管理员身份运行本脚本。右键 PowerShell -> 以管理员身份运行。"
    exit 1
}

if (-not $Workspace) {
    $Workspace = (Get-Location).Path
}
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
if (-not $Password) {
    $Password = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
}

$credDir = Join-Path $Workspace ".agent"
New-Item -ItemType Directory -Force -Path $credDir | Out-Null

# 1) 创建组与用户
if (-not (Get-LocalGroup -Name $GroupName -ErrorAction SilentlyContinue)) {
    New-LocalGroup -Name $GroupName -Description "Agent 沙箱低权限用户组" | Out-Null
    Write-Host "[OK] 创建组 $GroupName"
}

$Users = @{
    "CodexSandboxOffline" = "Agent 沙箱用户（禁止出站网络）"
    "CodexSandboxOnline"  = "Agent 沙箱用户（允许出站网络）"
}
foreach ($name in $Users.Keys) {
    if (-not (Get-LocalUser -Name $name -ErrorAction SilentlyContinue)) {
        New-LocalUser -Name $name -Password (ConvertTo-SecureString $Password -AsPlainText -Force) `
            -Description $Users[$name] -AccountNeverExpires -PasswordNeverExpires | Out-Null
        Write-Host "[OK] 创建用户 $name"
    }
    Add-LocalGroupMember -Group $GroupName -Member $name -ErrorAction SilentlyContinue
    Write-Host "[OK] 用户 $name 加入 $GroupName"
}

# 2) 合成 SID：不存在才创建，之后 ACL/restricted token 共用同一 SID
$sidFile = Join-Path $credDir "sandbox-write.sid"
$sid = ""
if (Test-Path -LiteralPath $sidFile) {
    $sid = (Get-Content -LiteralPath $sidFile -Raw).Trim()
}
if (-not $sid) {
    $sid = New-SandboxWriteSid
    [System.IO.File]::WriteAllText($sidFile, $sid)
    Write-Host "[OK] 创建合成 SID $sid -> $sidFile"
} else {
    Write-Host "[OK] 复用合成 SID $sid"
}

# 3) DPAPI 加密保存凭据（CurrentUser scope，沙箱用户无法解密）
Add-Type -AssemblyName System.Security
$credJson = @{
    offline_username = "CodexSandboxOffline"
    online_username  = "CodexSandboxOnline"
    password         = $Password
} | ConvertTo-Json -Compress
$credBytes = [System.Text.Encoding]::UTF8.GetBytes($credJson)
$entropy = [System.Text.Encoding]::UTF8.GetBytes("work-agent-sandbox-v1")
$protected = [System.Security.Cryptography.ProtectedData]::Protect(
    $credBytes,
    $entropy,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)
$credFile = Join-Path $credDir "sandbox_creds.bin"
[System.IO.File]::WriteAllBytes($credFile, $protected)
Write-Host "[OK] DPAPI 凭据写入 $credFile"

# 4) 凭据目录与文件仅当前用户 / SYSTEM / Administrators 可访问
$currentUser = $env:USERNAME
icacls $credDir /inheritance:r /grant:r "${currentUser}:(R,W,D)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null
icacls $sidFile /inheritance:r /grant:r "${currentUser}:(R,W)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null
icacls $credFile /inheritance:r /grant:r "${currentUser}:(R,W)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null
Write-Host "[OK] 凭据/ SID 文件 ACL 已收紧"

# 5) 沙箱用户读权限：工作区 + 常见目录（对齐 Codex read ACL 层）
icacls $Workspace /grant "CodexSandboxUsers:(OI)(CI)(RX)" /T | Out-Null
Write-Host "[OK] workspace 授予 CodexSandboxUsers 读/执行（递归）"
$readDirs = @(
    [Environment]::GetFolderPath("UserProfile"),
    "C:\Windows",
    "C:\Program Files",
    "C:\Program Files (x86)",
    "C:\ProgramData"
)
$readDirs += @("D:\Program Files\Git", "D:\anaconda3") | Where-Object { Test-Path -LiteralPath $_ }
$readDirs += $ReadDirs
foreach ($dir in $readDirs) {
    if ($dir -and (Test-Path -LiteralPath $dir)) {
        icacls $dir /grant "CodexSandboxUsers:(OI)(CI)(RX)" | Out-Null
        Write-Host "[OK] 常见目录授予读/执行: $dir"
    }
}

# 6) sandbox-write SID 的写 ACL：workspace / writable_roots
$writeRoots = @($Workspace) + @($WritableRoots)
foreach ($root in $writeRoots) {
    if (-not (Test-Path -LiteralPath $root)) {
        Write-Host "[WARN] writable root 不存在，跳过 ACL: $root"
        continue
    }
    icacls $root /grant "*${sid}:(OI)(CI)(W,D,AD,DC,X)" /T | Out-Null
    Write-Host "[OK] 授予 sandbox-write Write/Execute/Delete: $root"
}

# 7) 可写区域内必须只读的路径：Deny Write（sandbox-write SID）+ 阻止沙箱用户读 .agent
$denyNames = @(".git", ".codex", ".agents", ".agent")
foreach ($root in $writeRoots) {
    foreach ($name in $denyNames) {
        $target = Join-Path $root $name
        if (Test-Path -LiteralPath $target) {
            icacls $target /deny "*${sid}:(OI)(CI)(W,D,AD,DC)" /T | Out-Null
            Write-Host "[OK] Deny Write: $target"
        }
    }
}
foreach ($root in $writeRoots) {
    $agentDir = Join-Path $root ".agent"
    if (Test-Path -LiteralPath $agentDir) {
        icacls $agentDir /deny "*${sid}:(OI)(CI)(W,D,AD,DC)" /T | Out-Null
        icacls $agentDir /deny "CodexSandboxUsers:(OI)(CI)(R,W,D)" /T | Out-Null
        Write-Host "[OK] Deny Write / 禁止沙箱读: $agentDir"
    }
}

# 8) 防火墙：仅 CodexSandboxOffline 禁止出站（规则按用户 SID 匹配）
$ruleName = "Codex Sandbox Offline - Block Outbound"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
$offUser = Get-LocalUser -Name "CodexSandboxOffline"
$offSid = $offUser.SID.Value
New-NetFirewallRule -DisplayName $ruleName -Direction Outbound -Action Block `
    -LocalUser $offSid -Program Any -Profile Any -RemoteAddress Any | Out-Null
Write-Host "[OK] 防火墙规则（按用户 SID $offSid 绑定）"

# 9) 清理旧版明文凭据
$legacyCred = Join-Path $credDir "sandbox_user.txt"
if (Test-Path -LiteralPath $legacyCred) {
    Remove-Item -LiteralPath $legacyCred -Force
    Write-Host "[OK] 删除旧版明文凭据 $legacyCred"
}

Write-Host ""
Write-Host "初始化完成。运行时 Agent 将："
Write-Host "  - read-only / workspace-write : CodexSandboxOffline + write-restricted token（断网）"
Write-Host "  - danger-full                 : 脱离沙箱，以真实用户运行（联网）"
