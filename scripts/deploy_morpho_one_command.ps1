# One-command MorphoFlashLiquidator deploy.
# Sepolia (84532) is the default. Constructor starts paused; script also calls pause().
# NEVER unpause. NEVER send liquidate txs. Never print private keys.
#
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_morpho_one_command.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_morpho_one_command.ps1 -Network mainnet
param(
    [ValidateSet("sepolia", "mainnet")]
    [string]$Network = "sepolia",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Get-DotEnvValue([string]$Name) {
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match "^\s*$([regex]::Escape($Name))=(.*)$") {
            $v = $Matches[1].Trim().Trim('"').Trim("'")
            if ($v -ne "") { return $v }
        }
    }
    return $null
}

function Get-Secret([string[]]$Names) {
    foreach ($n in $Names) {
        $fromProc = [Environment]::GetEnvironmentVariable($n)
        if ($fromProc -and $fromProc.Trim() -ne "") {
            return @{ Name = $n; Value = $fromProc.Trim() }
        }
        $fromFile = Get-DotEnvValue $n
        if ($fromFile) { return @{ Name = $n; Value = $fromFile } }
    }
    return $null
}

$chainId = if ($Network -eq "sepolia") { 84532 } else { 8453 }
$keyNames = if ($Network -eq "sepolia") {
    @("BASE_SEPOLIA_PRIVATE_KEY", "SEPOLIA_PRIVATE_KEY", "DEPLOYER_KEY", "MORPHO_PRIVATE_KEY", "PRIVATE_KEY")
} else {
    @("BASE_PRIVATE_KEY", "DEPLOYER_KEY", "MORPHO_PRIVATE_KEY", "PRIVATE_KEY")
}

$key = Get-Secret $keyNames
if (-not $key) {
    Write-Host "BLOCKER: no deploy private key ($Network). Looked for $($keyNames -join ', ')."
    Write-Host "Fill PRIVATE_KEY (or BASE_SEPOLIA_PRIVATE_KEY) in .env then re-run this script."
    exit 2
}
Write-Host "Using deploy key from $($key.Name) (value not printed)."

if ($Network -eq "sepolia") {
    $rpc = Get-DotEnvValue "BASE_SEPOLIA_RPC_URL"
    if (-not $rpc) { $rpc = "https://base-sepolia-rpc.publicnode.com" }
} else {
    $rpc = Get-DotEnvValue "BASE_RPC_URL"
    if (-not $rpc) { $rpc = Get-DotEnvValue "BASE_HTTP_RPC_URL" }
    if (-not $rpc) { $rpc = "https://mainnet.base.org" }
}

$env:PRIVATE_KEY = $key.Value
if (-not $env:BASE_SEPOLIA_RPC_URL) {
    $env:BASE_SEPOLIA_RPC_URL = $rpc
}

$forgeArgs = @(
    "script", "scripts/DeployMorphoFlashLiquidator.s.sol:DeployMorphoFlashLiquidator",
    "--rpc-url", $rpc,
    "--chain", "$chainId",
    "--private-key", $key.Value
)
if ($DryRun) {
    Write-Host "Dry-run (no --broadcast) on chain $chainId"
} else {
    $forgeArgs += "--broadcast"
    Write-Host "Broadcasting MorphoFlashLiquidator to $Network chainId=$chainId"
}

& forge @forgeArgs
$code = $LASTEXITCODE
# Drop key from this process as soon as forge returns.
$env:PRIVATE_KEY = ""
$key = $null
if ($code -ne 0) { exit $code }

if ($DryRun) { exit 0 }

$broadcast = Join-Path $Root "broadcast\DeployMorphoFlashLiquidator.s.sol\$chainId\run-latest.json"
if (-not (Test-Path $broadcast)) {
    Write-Host "Deploy finished but broadcast json missing: $broadcast"
    exit 3
}
$py = Join-Path $Root ".venv-run\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $Root "scripts\check_morpho_liq_view.py") --record-broadcast $broadcast --network $Network
exit $LASTEXITCODE
