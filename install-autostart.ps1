param([switch]$Remove)
# Adds (or with -Remove, deletes) a Startup-folder shortcut that launches Lookout at sign-in.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Lookout.lnk'

if ($Remove) {
    Remove-Item $lnk -ErrorAction SilentlyContinue
    "Removed $lnk"
    return
}

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $here '.venv\Scripts\pythonw.exe'
$s.Arguments = '"' + (Join-Path $here 'lookout.py') + '"'
$s.WorkingDirectory = $here
$s.Description = 'Lookout - camera person-detection popups'
$s.Save()
"Created $lnk"
