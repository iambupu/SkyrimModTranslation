[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [string]$OutputDir = "work/normalized"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath.TrimEnd('\')

function Test-InProjectRoot {
    param([string]$FullPath)
    $normalized = [System.IO.Path]::GetFullPath($FullPath).TrimEnd('\')
    return $normalized.Equals($script:ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $normalized.StartsWith($script:ProjectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Resolve-ProjectInputPath {
    param([string]$PathValue, [string]$Name)
    $candidate = if ([System.IO.Path]::IsPathRooted($PathValue)) { $PathValue } else { Join-Path $script:ProjectRoot $PathValue }
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "$Name does not exist: $PathValue"
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).ProviderPath
    if (-not (Test-InProjectRoot $resolved)) {
        throw "$Name must be inside project root: $PathValue"
    }
    return $resolved
}

function Resolve-ProjectDirectory {
    param([string]$PathValue, [string]$Name)
    $candidate = if ([System.IO.Path]::IsPathRooted($PathValue)) { $PathValue } else { Join-Path $script:ProjectRoot $PathValue }
    $full = [System.IO.Path]::GetFullPath($candidate)
    if (-not (Test-InProjectRoot $full)) {
        throw "$Name must be inside project root: $PathValue"
    }
    if (-not (Test-Path -LiteralPath $full)) {
        New-Item -ItemType Directory -Path $full | Out-Null
    }
    $resolved = (Resolve-Path -LiteralPath $full).ProviderPath
    if (-not (Test-InProjectRoot $resolved)) {
        throw "$Name must be inside project root: $PathValue"
    }
    return $resolved
}

function Test-JsonLines {
    param([string[]]$Lines)
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ([string]::IsNullOrWhiteSpace($Lines[$i])) {
            Write-Output "Line $($i + 1) is empty. JSONL requires one JSON object per line."
            return $false
        }
        try {
            $null = $Lines[$i] | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            Write-Output "Line $($i + 1) is not valid JSON: $($_.Exception.Message)"
            return $false
        }
    }
    return $true
}

$inputFull = Resolve-ProjectInputPath -PathValue $InputPath -Name "InputPath"
$outputFull = Resolve-ProjectDirectory -PathValue $OutputDir -Name "OutputDir"
$lines = [System.IO.File]::ReadAllLines($inputFull)

if (-not (Test-JsonLines -Lines $lines)) {
    Write-Output "Input is not recognized as JSONL. Provide a LexTranslator or xTranslator export sample before adding a converter."
    exit 1
}

$baseName = [System.IO.Path]::GetFileNameWithoutExtension($inputFull)
$outputPath = Join-Path $outputFull "$baseName.normalized.jsonl"
if ([System.IO.Path]::GetFullPath($outputPath) -ieq [System.IO.Path]::GetFullPath($inputFull)) {
    throw "Output path would overwrite the original file."
}

[System.IO.File]::Copy($inputFull, $outputPath, $true)
Write-Output "JSONL input copied without modification."
Write-Output "Input: $inputFull"
Write-Output "Output: $outputPath"

