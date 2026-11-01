# ==============================================================================
# Script: repair_dns.ps1
# Purpose: Configure Google Public DNS on the active Wi-Fi adapter and flush DNS
# Target: Windows 11 - Wi-Fi Adapter (Realtek RTL8852BE WiFi 6)
# ==============================================================================

# Ensure script is running with elevated administrative privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "ERROR: This script must be executed in an elevated PowerShell session (Run as Administrator)."
    exit 1
}

$adapterAlias = "Wi-Fi"
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [CFRN Network Repair] Configuring Google Public DNS" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Inspect and log current settings
$currentDNS = (Get-DnsClientServerAddress -InterfaceAlias $adapterAlias -AddressFamily IPv4).ServerAddresses
Write-Host "[1/5] Inspecting adapter '$adapterAlias'..." -ForegroundColor Yellow
Write-Host "      Current IPv4 DNS: $($currentDNS -join ', ')"

# Backup configuration to file for rollback reference
$backupFile = "$PSScriptRoot\dns_backup.json"
$backupData = [PSCustomObject]@{
    InterfaceAlias  = $adapterAlias
    OriginalDNS     = $currentDNS
    Timestamp       = (Get-Date).ToString("o")
}
$backupData | ConvertTo-Json | Set-Content -Path $backupFile -Encoding UTF8
Write-Host "      Backup saved to: $backupFile" -ForegroundColor Green

# 2. Configure Google Public DNS (8.8.8.8, 8.8.4.4)
Write-Host "`n[2/5] Setting IPv4 DNS to Google Public DNS (8.8.8.8, 8.8.4.4)..." -ForegroundColor Yellow
try {
    Set-DnsClientServerAddress -InterfaceAlias $adapterAlias -ServerAddresses ("8.8.8.8", "8.8.4.4") -ErrorAction Stop
    Write-Host "      DNS addresses applied successfully." -ForegroundColor Green
} catch {
    Write-Error "      Failed to set DNS addresses: $($_.Exception.Message)"
    exit 1
}

# 3. Flush DNS resolver cache
Write-Host "`n[3/5] Flushing Windows DNS resolver cache..." -ForegroundColor Yellow
$flushResult = ipconfig /flushdns
Write-Host "      $flushResult" -ForegroundColor Green

# 4. Verify configuration on adapter
Write-Host "`n[4/5] Verifying configured DNS on '$adapterAlias'..." -ForegroundColor Yellow
$verifiedDNS = (Get-DnsClientServerAddress -InterfaceAlias $adapterAlias -AddressFamily IPv4).ServerAddresses
Write-Host "      Active IPv4 DNS: $($verifiedDNS -join ', ')" -ForegroundColor Cyan

if (($verifiedDNS -contains "8.8.8.8") -and ($verifiedDNS -contains "8.8.4.4")) {
    Write-Host "      Verification SUCCESS: Google DNS is actively bound." -ForegroundColor Green
} else {
    Write-Warning "      Verification WARNING: Expected 8.8.8.8, 8.8.4.4 but found $($verifiedDNS -join ', ')"
}

# 5. Diagnostic DNS & HTTPS connectivity checks
Write-Host "`n[5/5] Running resolution and latency diagnostics..." -ForegroundColor Yellow

# Test system resolution
$sw1 = [System.Diagnostics.Stopwatch]::StartNew()
$res1 = Resolve-DnsName -Name "google.com" -ErrorAction SilentlyContinue
$sw1.Stop()
Write-Host "      Resolve 'google.com' via system resolver: $($sw1.ElapsedMilliseconds) ms" -ForegroundColor White

$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
$res2 = Resolve-DnsName -Name "microsoft.com" -ErrorAction SilentlyContinue
$sw2.Stop()
Write-Host "      Resolve 'microsoft.com' via system resolver: $($sw2.ElapsedMilliseconds) ms" -ForegroundColor White

# Direct query against 8.8.8.8
$resGoogleDirect = Resolve-DnsName -Name "google.com" -Server "8.8.8.8" -ErrorAction SilentlyContinue
if ($resGoogleDirect) {
    Write-Host "      Direct query to 8.8.8.8: SUCCESS" -ForegroundColor Green
} else {
    Write-Warning "      Direct query to 8.8.8.8: FAILED"
}

# HTTPS connectivity test
try {
    $web = Invoke-WebRequest -Uri "https://www.google.com" -UseBasicParsing -TimeoutSec 5
    Write-Host "      HTTPS Reachability (https://www.google.com): Status $($web.StatusCode) OK" -ForegroundColor Green
} catch {
    Write-Warning "      HTTPS Reachability test failed: $_"
}

Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host " Network Resolution Repair Completed Successfully!" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
