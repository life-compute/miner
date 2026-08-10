# ============================================================
#  LIFE Compute Miner Installer for Windows (PowerShell)
#  Your GPU could help cure cancer. Earn $LIFE tokens.
# ============================================================
#Requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ─── Colors & Helpers ────────────────────────────────────────
function Write-Banner {
    $lines = @(
        "  ██╗     ██╗███████╗███████╗     ██████╗ ██████╗ ███╗   ███╗██████╗ ██╗   ██╗████████╗███████╗",
        "  ██║     ██║██╔════╝██╔════╝    ██╔════╝██╔═══██╗████╗ ████║██╔══██╗██║   ██║╚══██╔══╝██╔════╝",
        "  ██║     ██║█████╗  █████╗      ██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║   ██║   █████╗  ",
        "  ██║     ██║██╔══╝  ██╔══╝      ██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██║   ██║   ██║   ██╔══╝  ",
        "  ███████╗██║██║     ███████╗    ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ╚██████╔╝   ██║   ███████╗",
        "  ╚══════╝╚═╝╚═╝     ╚══════╝     ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝      ╚═════╝    ╚═╝   ╚══════╝"
    )
    Write-Host ""
    foreach ($line in $lines) {
        Write-Host $line -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "         ✦  Your GPU could help cure cancer. Earn `$LIFE tokens.  ✦" -ForegroundColor Cyan
    Write-Host "                        Decentralized Drug Discovery Network" -ForegroundColor DarkGray
    Write-Host ""
}

function Write-Step {
    param([int]$Num, [string]$Title)
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Magenta
    Write-Host "  Step ${Num}: ${Title}" -ForegroundColor Magenta
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Magenta
}

function Write-Info    { param([string]$Msg) Write-Host "  ℹ  $Msg" -ForegroundColor Cyan }
function Write-Success { param([string]$Msg) Write-Host "  ✔  $Msg" -ForegroundColor Green }
function Write-Warn    { param([string]$Msg) Write-Host "  ⚠  $Msg" -ForegroundColor Yellow }
function Write-Err     { param([string]$Msg) Write-Host "  ✖  $Msg" -ForegroundColor Red }
function Stop-Install  { param([string]$Msg) Write-Err $Msg; exit 1 }

# ─── Config ─────────────────────────────────────────────────
$LifeDir     = Join-Path $env:USERPROFILE ".life-compute"
$DockerImage = "ghcr.io/life-compute/miner:latest"
$ServiceName = "life-compute-miner"
$WalletFile  = Join-Path $LifeDir "wallet.json"
$ConfigFile  = Join-Path $LifeDir "config.json"

# ─── Banner ─────────────────────────────────────────────────
Write-Banner
Write-Host "  Installer version 1.0.0  •  $(Get-Date -Format 'yyyy-MM-dd')" -ForegroundColor DarkGray
Write-Host ""

# ════════════════════════════════════════════════════════════
# STEP 1 — DOWNLOAD & PREREQUISITES CHECK
# ════════════════════════════════════════════════════════════
Write-Step 1 "Download & Prerequisites"

Write-Info "Checking system prerequisites..."

# Docker
try {
    $dockerVer = (docker --version 2>&1) -replace "Docker version ",""
    Write-Success "Docker found: $dockerVer"
} catch {
    Stop-Install "Docker is not installed. Install Docker Desktop from https://docs.docker.com/desktop/windows/install/"
}

# Docker daemon
try {
    docker info 2>&1 | Out-Null
    Write-Success "Docker daemon is running"
} catch {
    Stop-Install "Docker daemon is not running. Start Docker Desktop and re-run this installer."
}

# Python 3.10+
try {
    $pyVer = python --version 2>&1
    if ($pyVer -match "Python (\d+\.\d+)") {
        $verNum = [version]$Matches[1]
        if ($verNum -ge [version]"3.10") {
            Write-Success "Python $($Matches[1]) found"
        } else {
            Write-Warn "Python $($Matches[1]) found — 3.10+ is recommended. Download from https://python.org"
        }
    }
} catch {
    Write-Warn "Python not found. Dashboard features will be limited."
}

# NVIDIA GPU
$GpuOk = $false
try {
    $gpuInfo = nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1
    Write-Success "NVIDIA GPU detected: $gpuInfo"
    $GpuOk = $true
} catch {
    Write-Warn "No NVIDIA GPU detected. Miner will run in CPU mode (slower)."
    Write-Warn "For best performance, use a machine with an NVIDIA GPU."
}

Write-Info "Pulling Docker image: $DockerImage"
Write-Info "(This may take a few minutes on first run...)"
try {
    docker pull $DockerImage
    Write-Success "Docker image pulled successfully"
} catch {
    Write-Warn "Could not pull image from registry. It may not be published yet."
    Write-Warn "Continue anyway — build locally with: docker build -t $DockerImage ."
}

New-Item -ItemType Directory -Force -Path $LifeDir | Out-Null
Write-Success "Created config directory: $LifeDir"

# ════════════════════════════════════════════════════════════
# STEP 2 — CONNECT SOLANA WALLET
# ════════════════════════════════════════════════════════════
Write-Step 2 "Connect Your Solana Wallet"

$SkipWallet = $false
if (Test-Path $WalletFile) {
    Write-Info "Existing wallet found at $WalletFile"
    Write-Host "  Do you want to use the existing wallet or set up a new one?" -ForegroundColor Yellow
    Write-Host "    [1] Use existing wallet"
    Write-Host "    [2] Replace with a new/different wallet"
    $walletChoice = Read-Host "  Choice [1]"
    if ([string]::IsNullOrEmpty($walletChoice)) { $walletChoice = "1" }
    if ($walletChoice -eq "1") {
        Write-Success "Using existing wallet"
        $SkipWallet = $true
    }
}

if (-not $SkipWallet) {
    Write-Host ""
    Write-Host "  How would you like to set up your wallet?" -ForegroundColor White
    Write-Host "    [1] Enter an existing Solana wallet address"
    Write-Host "    [2] Generate a new keypair (recommended for first-time users)"
    $choice = Read-Host "  Choice [2]"
    if ([string]::IsNullOrEmpty($choice)) { $choice = "2" }

    if ($choice -eq "1") {
        $walletAddr = Read-Host "  Enter your Solana wallet address (public key)"
        if ([string]::IsNullOrEmpty($walletAddr)) {
            Stop-Install "Wallet address cannot be empty."
        }
        $walletData = @{
            pubkey = $walletAddr
            type   = "provided"
            note   = "Pubkey-only entry. The private key is managed by your own wallet (Phantom, Solflare, etc.)."
        } | ConvertTo-Json
        Set-Content -Path $WalletFile -Value $walletData
        Write-Success "Wallet address saved: $walletAddr"
        Write-Info "Note: reward claims require signing — connect your wallet in the dashboard."

    } elseif ($choice -eq "2") {
        Write-Info "Generating a new Solana keypair via Python..."
        $pyScript = @"
import json, os, sys
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base58
    pk = Ed25519PrivateKey.generate()
    priv = list(pk.private_bytes_raw())
    pub  = list(pk.public_key().public_bytes_raw())
    keypair = priv + pub
    pubkey  = base58.b58encode(bytes(pub)).decode()
except ImportError:
    import random
    keypair = [random.randint(0,255) for _ in range(64)]
    pubkey  = 'InstallSolanaCLI_for_real_keypair'
path = os.path.join(os.path.expanduser('~'), '.life-compute', 'wallet.json')
with open(path, 'w') as f:
    json.dump(keypair, f)
print(f'Public key: {pubkey}')
print('IMPORTANT: Back up ~/.life-compute/wallet.json immediately!')
"@
        python -c $pyScript
        Write-Success "Keypair generated and saved to $WalletFile"
        Write-Host "  ⚠  IMPORTANT: Back up $WalletFile — it contains your private key!" -ForegroundColor Red
    } else {
        Stop-Install "Invalid choice."
    }
}

# ════════════════════════════════════════════════════════════
# STEP 3 — START & CONFIGURE AUTO-START
# ════════════════════════════════════════════════════════════
Write-Step 3 "Start Miner & Enable Auto-Start on Boot"

$configData = @{
    wallet_path              = $WalletFile
    rpc_url                  = "https://api.devnet.solana.com"
    target_refresh_interval  = 300
    log_level                = "INFO"
    stats_output             = (Join-Path $LifeDir "stats.json")
} | ConvertTo-Json -Depth 3
Set-Content -Path $ConfigFile -Value $configData
Write-Success "Config written to $ConfigFile"

# Stop any existing container
try { docker rm -f $ServiceName 2>&1 | Out-Null } catch {}

$gpuArg = if ($GpuOk) { "--gpus all" } else { "" }
$dockerRunCmd = "docker run -d --name $ServiceName --restart unless-stopped $gpuArg -v ${LifeDir}:/root/.life-compute -p 8765:8765 $DockerImage".Trim()

Write-Info "Starting miner container..."
try {
    Invoke-Expression $dockerRunCmd | Out-Null
    Write-Success "Miner container started (name: $ServiceName)"
} catch {
    Write-Warn "Could not start container (image may not be available locally)."
    Write-Warn "Once the image is available, run:"
    Write-Host ""
    Write-Host "  $dockerRunCmd" -ForegroundColor Cyan
    Write-Host ""
}

# ─── Task Scheduler auto-start ──────────────────────────────
Write-Info "Setting up Windows Task Scheduler for auto-start on boot..."
try {
    $taskAction  = New-ScheduledTaskAction -Execute "docker" -Argument "start $ServiceName"
    $taskTrigger = New-ScheduledTaskTrigger -AtStartup
    $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName "LIFE Compute Miner" `
        -Action $taskAction -Trigger $taskTrigger `
        -Settings $taskSettings -Principal $taskPrincipal `
        -Description "LIFE Compute Miner — Decentralized Cancer Drug Discovery" `
        -Force | Out-Null
    Write-Success "Task Scheduler entry created — miner auto-starts on every boot"
} catch {
    Write-Warn "Could not create Task Scheduler entry (may need administrator privileges)."
    Write-Warn "Run this installer as Administrator to enable auto-start."
}

# ─── Summary ─────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host "  ✦  Installation Complete!  ✦" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host ""
Write-Host "  Your GPU could help cure cancer. Earn `$LIFE tokens." -ForegroundColor White
Write-Host ""
Write-Host "  Miner logs:   docker logs -f $ServiceName" -ForegroundColor Cyan
Write-Host "  Dashboard:    http://localhost:8765" -ForegroundColor Cyan
Write-Host "  Config:       $ConfigFile" -ForegroundColor Cyan
Write-Host "  Wallet:       $WalletFile" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Join the community: https://discord.gg/life-compute" -ForegroundColor DarkGray
Write-Host "  Docs: https://docs.life-compute.io" -ForegroundColor DarkGray
Write-Host ""
