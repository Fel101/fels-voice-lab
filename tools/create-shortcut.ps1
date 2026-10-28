$ErrorActionPreference = 'Stop'
$labRoot = Split-Path -Parent $PSScriptRoot
$windowPython = Join-Path $labRoot '.venv-desktop\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $windowPython)) {
    throw 'Run setup-desktop.bat first.'
}
$shell = New-Object -ComObject WScript.Shell
$desktopPath = [Environment]::GetFolderPath('DesktopDirectory')
$linkPath = Join-Path $desktopPath "Fel's Voice Lab.lnk"
$legacyPath = Join-Path $desktopPath 'Voice Lab.lnk'
$desktopScript = Join-Path $labRoot 'desktop.py'
# Rename only the shortcut owned by this installation; preserve unrelated links.
if ((-not (Test-Path -LiteralPath $linkPath)) -and (Test-Path -LiteralPath $legacyPath)) {
    $legacyLink = $shell.CreateShortcut($legacyPath)
    if (($legacyLink.TargetPath -eq $windowPython) -and ($legacyLink.Arguments -eq ('"' + $desktopScript + '"'))) {
        Move-Item -LiteralPath $legacyPath -Destination $linkPath
    }
}
if (Test-Path -LiteralPath $linkPath) {
    $existingLink = $shell.CreateShortcut($linkPath)
    if (($existingLink.TargetPath -ne $windowPython) -or ($existingLink.Arguments -ne ('"' + $desktopScript + '"'))) {
        throw "A different Fel's Voice Lab shortcut already exists at $linkPath. It was preserved."
    }
}
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $windowPython
$link.Arguments = '"' + $desktopScript + '"'
$link.WorkingDirectory = $labRoot
$link.IconLocation = (Join-Path $labRoot 'assets\voice-lab.ico') + ',0'
$link.Description = "Fel's Voice Lab - local voice design, casting and dialogue studio"
$link.Save()
Write-Output $linkPath
