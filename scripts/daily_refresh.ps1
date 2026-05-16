# Refresh Investing.com caches on login, but only if it's been >20 hours
# since the last successful run. Prevents wasted runs on multiple logins
# per day.

$ErrorActionPreference = "Stop"
$repo = "C:\Users\yanae\Desktop\Swing Trading\edgefinder"
$log  = Join-Path $repo "scripts\daily_refresh.log"
$marker = Join-Path $repo "scripts\.last_refresh"

Set-Location $repo

# Skip if we ran successfully in the last 20 hours
if (Test-Path $marker) {
    $age = (Get-Date) - (Get-Item $marker).LastWriteTime
    if ($age.TotalHours -lt 20) {
        "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Append
        "Skipped: last refresh was $([math]::Round($age.TotalHours,1))h ago." | Tee-Object -FilePath $log -Append
        exit 0
    }
}

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Append

try {
    python scripts\refresh_investing.py 2>&1 | Tee-Object -FilePath $log -Append

    $changes = git status --porcelain data/cache/
    if ($changes) {
        git add data/cache/ 2>&1 | Tee-Object -FilePath $log -Append
        git commit -m "Investing refresh (auto on login)" 2>&1 | Tee-Object -FilePath $log -Append
        git push 2>&1 | Tee-Object -FilePath $log -Append
        "Pushed updates." | Tee-Object -FilePath $log -Append
    } else {
        "No cache changes - nothing to push." | Tee-Object -FilePath $log -Append
    }

    # Mark successful run
    Set-Content -Path $marker -Value (Get-Date -Format 'o')
}
catch {
    "ERROR: $_" | Tee-Object -FilePath $log -Append
    exit 1
}