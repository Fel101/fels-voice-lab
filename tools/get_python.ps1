# Fetches the Python the Lab runs on into python\, for a computer with no Python 3.12. setup.bat runs it; every environment the
# Lab makes is then built from python\python.exe. What to fetch, and its SHA-256, are in catalog\python_runtime.json.
# Nothing is half-written: the download and its unpacking happen in python.partial\, and python\ appears only when complete.
# Exit status 1, with the reason, when the download fails or does not match its SHA-256.
param(
    [string]$Catalog = (Join-Path $PSScriptRoot "..\catalog\python_runtime.json"),
    [string]$Destination = (Join-Path $PSScriptRoot "..\python")
)
$ErrorActionPreference = "Stop"
# Windows PowerShell draws its progress bar so slowly that it slows the download itself.
$ProgressPreference = "SilentlyContinue"
# Windows PowerShell 5.1 may offer only older TLS versions, which nuget.org refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$runtime = Get-Content -Raw -LiteralPath $Catalog | ConvertFrom-Json
$Destination = [IO.Path]::GetFullPath($Destination)
$partial = "$Destination.partial"
$package = Join-Path $partial "package.zip"
$unpacked = Join-Path $partial "python"

if (Test-Path -LiteralPath (Join-Path $Destination "python.exe")) {
    Write-Host "The Lab's own Python is already in $Destination."
    exit 0
}
if (Test-Path -LiteralPath $Destination) {
    Write-Host "There is a folder $Destination without python.exe in it. Setup does not change a folder it did not make: move or delete it, then run setup.bat again."
    exit 1
}

function Stop-Setup([string]$Why) {
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
    Write-Host $Why
    exit 1
}

# What an interrupted run left behind.
if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
New-Item -ItemType Directory -Path $partial | Out-Null

$megabytes = [Math]::Round($runtime.bytes / 1MB)
Write-Host ("Python 3.12 is not on this computer, so setup is downloading the Lab's own Python {0} ({1} MB, from {2}) into {3}." -f `
        $runtime.version, $megabytes, ([Uri]$runtime.url).Host, $Destination)
try {
    Invoke-WebRequest -UseBasicParsing -Uri $runtime.url -OutFile $package
} catch {
    Stop-Setup "The download of Python stopped: $($_.Exception.Message) Check the internet connection, then run setup.bat again."
}

$hash = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hash -ne $runtime.sha256.ToLowerInvariant()) {
    Stop-Setup ("The downloaded Python does not match its published SHA-256, so it was deleted and nothing was installed. " +
        "Run setup.bat again; if it happens again, something between this computer and $(([Uri]$runtime.url).Host) is changing the file.")
}

# The package keeps Python in one folder (tools/); the rest is NuGet's own bookkeeping.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$prefix = "$($runtime.folder)/"
$archive = [IO.Compression.ZipFile]::OpenRead($package)
try {
    foreach ($entry in $archive.Entries) {
        if (-not $entry.FullName.StartsWith($prefix) -or $entry.FullName.EndsWith("/")) { continue }
        $target = [IO.Path]::GetFullPath((Join-Path $unpacked $entry.FullName.Substring($prefix.Length)))
        if (-not $target.StartsWith("$unpacked\", [StringComparison]::OrdinalIgnoreCase)) {
            $archive.Dispose()
            Stop-Setup "The downloaded Python holds a file that would land outside its folder ($($entry.FullName)), so nothing was installed."
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target)
    }
} finally {
    $archive.Dispose()
}
if (-not (Test-Path -LiteralPath (Join-Path $unpacked "python.exe"))) {
    Stop-Setup "The downloaded package holds no python.exe, so nothing was installed."
}
Move-Item -LiteralPath $unpacked -Destination $Destination
Remove-Item -LiteralPath $partial -Recurse -Force
Write-Host "The Lab's own Python $($runtime.version) is in $Destination. Keep this folder: every environment the Lab makes is built on it."
exit 0
