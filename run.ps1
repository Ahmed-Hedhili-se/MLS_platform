# Start the MLS platform locally.
#
#   .\run.ps1            production server (waitress) on port 5000
#   .\run.ps1 -Ngrok     also open an ngrok tunnel
#   .\run.ps1 -Dev       Werkzeug dev server instead (never for students)

param(
    [switch]$Ngrok,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"

# The project is wherever this script lives, so the path does not go
# stale when the folder moves.
$Project = $PSScriptRoot

$Activate = Join-Path $Project ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $Activate)) {
    Write-Error "No virtualenv found at $Activate. Create one and install requirements.txt first."
}

if (-not (Test-Path (Join-Path $Project ".env"))) {
    Write-Error "No .env found in $Project. Copy .env.example and fill it in."
}

$env:MLS_DEV_SERVER = if ($Dev) { "1" } else { "" }

# ngrok terminates TLS, so tell the app its public scheme is https.
if ($Ngrok) { $env:MLS_URL_SCHEME = "https" }

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$Project'; .\.venv\Scripts\Activate.ps1; " +
    "`$env:MLS_DEV_SERVER='$($env:MLS_DEV_SERVER)'; " +
    "`$env:MLS_URL_SCHEME='$($env:MLS_URL_SCHEME)'; " +
    "python -m webapp.app"
)

if ($Ngrok) {

    Start-Sleep -Seconds 3

    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$Project'; ngrok http 5000"
    )
}
