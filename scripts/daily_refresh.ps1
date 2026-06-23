# Calendar-gated ("--due") Investing.com refresh, meant to run HOURLY from Task
# Scheduler. Fetches only the economic cells whose release window has passed
# (per data/cache/release_calendar.json), commits the changed caches, and pushes
# resiliently. Most hourly runs fetch nothing (no release due) and exit cheaply,
# which is the point: no blind sweep, far fewer Cloudflare 429s, and each
# indicator (e.g. Japan CPI) updates within ~an hour of release while the laptop
# is on. Trend/seasonality keep updating separately via GitHub Actions.
#
# To run the old full sweep manually: python scripts\refresh_investing.py
# To preview without fetching:        python scripts\refresh_investing.py --due --dry-run

$ErrorActionPreference = "Continue"
$repo = "C:\Users\yanae\Desktop\Swing Trading\edgefinder"
$log  = Join-Path $repo "scripts\daily_refresh.log"
$marker = Join-Path $repo "scripts\.last_refresh"

Set-Location $repo

function Log($m) { "$m" | Tee-Object -FilePath $log -Append }

# Guard against overlapping triggers (e.g. login + hourly firing close together).
if (Test-Path $marker) {
    $age = (Get-Date) - (Get-Item $marker).LastWriteTime
    if ($age.TotalMinutes -lt 45) {
        Log "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') === Skipped: ran $([math]::Round($age.TotalMinutes,0))m ago."
        exit 0
    }
}

Log "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

python scripts\refresh_investing.py --due 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { Log "ERROR: refresh exited $LASTEXITCODE"; exit 1 }

# Commit any changed caches (release_calendar.json included).
if (git status --porcelain data/cache/) {
    git add data/cache/ 2>&1 | Tee-Object -FilePath $log -Append
    git commit -m "Investing refresh (due)" 2>&1 | Tee-Object -FilePath $log -Append
}

# Resilient push: GitHub Actions commits hourly, so a plain push is usually
# rejected as non-fast-forward. Fetch, rebase (local caches win on conflict
# since Cloudflare sources can't be fetched in CI), push; retry to ride out CI
# pushing concurrently. Only acts when we actually have unpushed commits.
git fetch origin 2>&1 | Tee-Object -FilePath $log -Append
$ahead = git rev-list --count origin/main..HEAD
if (-not $ahead) { $ahead = 0 }

if ([int]$ahead -gt 0) {
    $pushed = $false
    for ($i = 1; $i -le 3; $i++) {
        git rebase -X theirs origin/main 2>&1 | Tee-Object -FilePath $log -Append
        if ($LASTEXITCODE -ne 0) {
            git rebase --abort 2>&1 | Tee-Object -FilePath $log -Append
            Log "Rebase failed (attempt $i), retrying."
            Start-Sleep -Seconds 5
            git fetch origin 2>&1 | Tee-Object -FilePath $log -Append
            continue
        }
        git push 2>&1 | Tee-Object -FilePath $log -Append
        if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
        Log "Push rejected (attempt $i), retrying."
        Start-Sleep -Seconds 5
        git fetch origin 2>&1 | Tee-Object -FilePath $log -Append
    }
    if ($pushed) { Log "Pushed due updates." }
    else { Log "ERROR: could not push after 3 attempts (will retry next run)." }
} else {
    Log "No due updates - nothing to push."
}

Set-Content -Path $marker -Value (Get-Date -Format 'o')
