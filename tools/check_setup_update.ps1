<#
Setup.exe over the release before it is an update (installer\FelsVoiceLab.iss). This installs the newest release older than
this version into a folder of its own, puts a character in its library, runs the given Setup.exe over it, and checks the
version, the files this version no longer has, the update record, the core packages against requirements\core.txt, and the
library.

  .\tools\check_setup_update.ps1 -Setup <the new Setup.exe>
  .\tools\check_setup_update.ps1 -Setup <the new Setup.exe> -Previous <the older release's Setup.exe> -Work <folder> -Uninstall

Run it from the repository's root, in a checkout of the commit the new Setup.exe was built from, with every tag: the files
this version no longer has are git's difference between the older release's tag and HEAD. Without -Previous, the older
release's Setup.exe is downloaded from GitHub with gh. The checks on GitHub run it twice: verify.yml on the Setup.exe it
builds, and release.yml on the one downloaded from the draft release before the draft is published. -Uninstall removes the
updated Lab at the end, for a run on a person's own computer.
#>
param(
  [Parameter(Mandatory = $true)] [string] $Setup,
  [string] $Previous = '',
  [string] $Work = '',
  [int] $Port = 8791,
  [switch] $Uninstall
)
$ErrorActionPreference = 'Stop'
if (-not $Work) { $Work = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() } }
$Setup = (Resolve-Path $Setup).Path
$version = (Select-String -Path app_version.py -Pattern '^VERSION = "(.+)"$').Matches[0].Groups[1].Value
$repository = (Select-String -Path app_version.py -Pattern '^REPOSITORY = "(.+)"$').Matches[0].Groups[1].Value
$previousTag = git tag --list 'v*' | Where-Object { $_ -match '^v\d+\.\d+\.\d+$' -and [version]$_.Substring(1) -lt [version]$version } |
  Sort-Object { [version]$_.Substring(1) } | Select-Object -Last 1
if (-not $previousTag) { "There is no release before $version to update from."; exit 0 }
if ($Previous) {
  $Previous = (Resolve-Path $Previous).Path
} else {
  $download = Join-Path $Work 'previous'
  gh release download $previousTag --repo $repository --pattern 'FelsVoiceLab-Setup-*.exe' --dir $download
  if ($LASTEXITCODE) { throw "The Setup.exe of $previousTag could not be downloaded" }
  $Previous = (Get-Item "$download\FelsVoiceLab-Setup-*.exe").FullName
}
$lab = Join-Path $Work 'UpdatedLab'
if (Test-Path $lab) { throw "$lab is already there; remove it, or give another folder with -Work" }
$silent = '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$lab`""
$url = "http://127.0.0.1:$Port"

function Start-Lab {
  $service = Start-Process "$lab\.venv\Scripts\python.exe" -ArgumentList 'serve.py', '--port', "$Port" -WorkingDirectory $lab -PassThru -NoNewWindow
  foreach ($try in 1..60) {
    try { $null = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; return $service } catch { Start-Sleep 1 }
  }
  Stop-Process -Id $service.Id -Force
  throw "The Lab in $lab did not answer /api/health"
}

$old = Start-Process $Previous -ArgumentList ($silent + "/LOG=`"$Work\previous-setup.log`"") -Wait -PassThru
if ($old.ExitCode) { throw "The Setup.exe of $previousTag ended with exit code $($old.ExitCode)" }
$service = Start-Lab
Invoke-RestMethod -Method Post "$url/api/characters" -ContentType 'application/json' -Body '{"name": "Kept through the update"}' | Out-Null
Stop-Process -Id $service.Id -Force
Start-Sleep 2
$new = Start-Process $Setup -ArgumentList ($silent + "/LOG=`"$Work\update-setup.log`"") -Wait -PassThru
if ($new.ExitCode) {
  Get-Content "$lab\out\install\setup-exe.log" -Tail 80 -ErrorAction SilentlyContinue
  throw "This Setup.exe over $previousTag ended with exit code $($new.ExitCode)"
}
$service = Start-Lab
$health = Invoke-RestMethod "$url/api/health"
$cast = Invoke-RestMethod "$url/api/cast"
Stop-Process -Id $service.Id -Force
if ($health.version -ne $version) { throw "The Lab says version $($health.version) after the update, not $version" }
if ($cast.characters.name -notcontains 'Kept through the update') { throw "The update lost the library's character" }
$dropped = @(git diff --diff-filter=D --name-only $previousTag HEAD)
foreach ($file in $dropped) { if (Test-Path (Join-Path $lab $file)) { throw "Setup left $file, which $version no longer has" } }
$record = Get-Content "$lab\out\update\last.json" -Raw | ConvertFrom-Json
if ($record.method -ne 'setup' -or $record.from -ne $previousTag.Substring(1) -or $record.to -ne $version) {
  throw "The update was not recorded as Setup's update from $previousTag to $version"
}
& "$lab\.venv\Scripts\python.exe" -m pip check
if ($LASTEXITCODE) { throw "pip check found a problem in the updated .venv" }
$pins = @'
import importlib.metadata, pathlib, sys
wrong = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    pin = line.split("#", 1)[0].split(";", 1)[0].strip()
    if "==" in pin:
        name, version = (part.strip() for part in pin.split("=="))
        if importlib.metadata.version(name) != version:
            wrong.append(f"{name} {importlib.metadata.version(name)}, pinned {version}")
print("Not as requirements/core.txt pins them: " + "; ".join(wrong) if wrong else "Every core package is as pinned.")
sys.exit(1 if wrong else 0)
'@
$pins | & "$lab\.venv\Scripts\python.exe" - "$lab\requirements\core.txt"
if ($LASTEXITCODE) { throw "The update left .venv's packages unlike requirements\core.txt" }
"Setup.exe updated $previousTag to $version`: the character kept, $($dropped.Count) dropped file(s) gone, the packages as pinned."

if ($Uninstall) {
  # The uninstaller runs from a copy of itself, so wait for it to remove its own files. The library stays, as it does for
  # anyone who uninstalls; remove the folder afterwards.
  Start-Process "$lab\unins000.exe" -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
  foreach ($try in 1..120) { if (-not (Test-Path "$lab\unins000.exe")) { break }; Start-Sleep 1 }
  if (Test-Path "$lab\unins000.exe") { throw "The uninstaller did not finish" }
  "Uninstalled the updated Lab; its library is still in $lab."
}
