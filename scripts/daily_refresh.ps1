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

function Log($m) { Add-Content -Path $log -Value $m }

# Run git, append its (stdout+stderr) output to the log as plain text, and
# return the exit code. Stringifying each line avoids PowerShell rendering
# git's normal stderr (e.g. "From github.com...") as red NativeCommandErrors.
function Git-Run {
    $out = & git @args 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    if ($out) { Add-Content -Path $log -Value $out }
    return $code
}

# Guard against overlapping triggers (e.g. login + hourly firing close together).
if (Test-Path $marker) {
    $age = (Get-Date) - (Get-Item $marker).LastWriteTime
    if ($age.TotalMinutes -lt 45) {
        Log "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') === Skipped: ran $([math]::Round($age.TotalMinutes,0))m ago."
        exit 0
    }
}

Log "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

python scripts\refresh_investing.py --due 2>&1 | ForEach-Object { "$_" } | Add-Content -Path $log
if ($LASTEXITCODE -ne 0) { Log "ERROR: refresh exited $LASTEXITCODE"; exit 1 }

# Commit any changed caches (release_calendar.json included).
if (git status --porcelain data/cache/) {
    Git-Run add data/cache/ | Out-Null
    Git-Run commit -m "Investing refresh (due)" | Out-Null
}

# Resilient push: GitHub Actions commits hourly, so a plain push is usually
# rejected as non-fast-forward. Fetch, rebase (local caches win on conflict;
# --autostash tolerates any stray unstaged file), push; retry to ride out CI
# pushing concurrently. Only acts when we have unpushed commits.
Git-Run fetch origin | Out-Null
$ahead = git rev-list --count origin/main..HEAD
if (-not $ahead) { $ahead = 0 }

if ([int]$ahead -gt 0) {
    $pushed = $false
    for ($i = 1; $i -le 3; $i++) {
        $rc = Git-Run rebase --autostash -X theirs origin/main
        if ($rc -ne 0) {
            Git-Run rebase --abort | Out-Null
            Log "Rebase failed (attempt $i), retrying."
            Start-Sleep -Seconds 5
            Git-Run fetch origin | Out-Null
            continue
        }
        if ((Git-Run push) -eq 0) { $pushed = $true; break }
        Log "Push rejected (attempt $i), retrying."
        Start-Sleep -Seconds 5
        Git-Run fetch origin | Out-Null
    }
    if ($pushed) { Log "Pushed due updates." }
    else { Log "ERROR: could not push after 3 attempts (will retry next run)." }
} else {
    Log "No due updates - nothing to push."
}

Set-Content -Path $marker -Value (Get-Date -Format 'o')
