param([switch]$NoBrowser, [int]$Port = 18765, [string]$PythonPath)
$ErrorActionPreference = 'Stop'

# Match tools/bootstrap.py; no personal interpreter path is stored in source.
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if ($env:PORTRAITLAB_WORK) {
    $workRoot = $env:PORTRAITLAB_WORK
    if (-not [IO.Path]::IsPathRooted($workRoot)) { $workRoot = Join-Path $projectRoot $workRoot }
} elseif ((Split-Path (Split-Path $projectRoot -Parent) -Leaf) -ceq 'outputs') {
    $workRoot = Join-Path (Split-Path (Split-Path $projectRoot -Parent) -Parent) 'work\portraitlab'
} else {
    $workRoot = Join-Path $projectRoot '.local'
}
$workRoot = [IO.Path]::GetFullPath($workRoot)
if ($env:PORTRAITLAB_STORAGE_ROOT) {
    $storageBoundary = [IO.Path]::GetFullPath($env:PORTRAITLAB_STORAGE_ROOT).TrimEnd('\', '/')
    if ($workRoot -ne $storageBoundary -and -not $workRoot.StartsWith($storageBoundary + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'PORTRAITLAB_WORK must be inside PORTRAITLAB_STORAGE_ROOT.'
    }
}
$env:PORTRAITLAB_WORK = $workRoot
$env:TEMP = Join-Path $workRoot 'temp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'

function Find-CompatiblePython([string]$Candidate, [string[]]$Prefix = @()) {
    if (-not $Candidate) { return $null }
    try {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            $executable = (Get-Item -LiteralPath $Candidate).FullName
        } else {
            $executable = (Get-Command $Candidate -CommandType Application -ErrorAction Stop).Source
        }
        # -S avoids site hooks during discovery; -B prevents bytecode writes.
        $probe = 'import sys,struct; sys.exit(42) if sys.version_info[:2] not in ((3,11),(3,12)) or struct.calcsize("P") != 8 else print(sys.executable)'
        $result = @(& $executable @Prefix -S -B -c $probe 2>$null)
        if ($LASTEXITCODE -eq 0 -and $result.Count -gt 0) {
            $resolved = ([string]$result[-1]).Trim()
            if (Test-Path -LiteralPath $resolved -PathType Leaf) { return $resolved }
        }
    } catch { }
    return $null
}

$selectedPython = $null
if ($PythonPath) {
    $selectedPython = Find-CompatiblePython $PythonPath
    if (-not $selectedPython) { throw '-PythonPath must identify a working 64-bit Python 3.11 or 3.12 executable. Python 3.12 is recommended.' }
} else {
    $existingVenv = Join-Path $workRoot 'venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $existingVenv -PathType Leaf) { $selectedPython = Find-CompatiblePython $existingVenv }
    foreach ($commandName in @('python.exe', 'python3.exe')) {
        if ($selectedPython) { break }
        $candidate = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.Source -notmatch '[\\/]WindowsApps[\\/]') {
            $selectedPython = Find-CompatiblePython $candidate.Source
        }
    }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    foreach ($version in @('-3.12', '-3.11')) {
        if ($selectedPython -or -not $launcher) { break }
        $selectedPython = Find-CompatiblePython $launcher.Source @($version)
    }
}
if (-not $selectedPython) { throw 'Python was not found. Run setup.ps1 -PythonPath <64-bit Python 3.12 executable> -DownloadModels first.' }
$toolArgs = @('-B', (Join-Path $PSScriptRoot 'tools\launch.py'), '--port', $Port)
if ($NoBrowser) { $toolArgs += '--no-browser' }
Push-Location -LiteralPath $projectRoot
try {
    & $selectedPython @toolArgs
    $toolExitCode = $LASTEXITCODE
} finally { Pop-Location }
exit $toolExitCode
