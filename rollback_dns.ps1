# ==============================================================================
# Script: rollback_dns.ps1
# Purpose: Revert DNS settings on Wi-Fi adapter back to original DHCP automatic assignment
# ==============================================================================

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "ERROR: This script must be executed in an elevated PowerShell session (Run as Administrator)."
    exit 1
}

$adapterAlias = "Wi-Fi"
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [CFRN Network Rollback] Restoring Original DNS Settings" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Check if backup file exists
$backupFile = "$PSScriptRoot\dns_backup.json"
if (Test-Path $backupFile) {
    $backupData = Get-Content $backupFile | ConvertFrom-Json
    Write-Host "Found backup from $($backupData.Timestamp). Original DNS: $($backupData.OriginalDNS -join ', ')" -ForegroundColor Yellow
}

# Reset DNS back to DHCP assignment
Write-Host "Resetting '$adapterAlias' DNS back to automatic DHCP..." -ForegroundColor Yellow
try {
    Set-DnsClientServerAddress -InterfaceAlias $adapterAlias -ResetServerAddresses -ErrorAction Stop
    Write-Host "DNS reset successful." -ForegroundColor Green
} catch {
    Write-Error "Failed to reset DNS: $($_.Exception.Message)"
    exit 1
}

# Flush DNS cache
$flushResult = ipconfig /flushdns
Write-Host "$flushResult" -ForegroundColor Green

# Verify
$restoredDNS = (Get-DnsClientServerAddress -InterfaceAlias $adapterAlias -AddressFamily IPv4).ServerAddresses
Write-Host "Active IPv4 DNS is now: $($restoredDNS -join ', ')" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
