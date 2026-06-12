[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [string]$OutputDir = "work/batches",

    [int]$BatchSize = 100
)

$ErrorActionPreference = "Stop"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
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

if ($BatchSize -lt 1) {
    throw "BatchSize must be greater than 0."
}

$inputFull = Resolve-ProjectInputPath -PathValue $InputPath -Name "InputPath"
$outputFull = Resolve-ProjectDirectory -PathValue $OutputDir -Name "OutputDir"
$lines = [System.IO.File]::ReadAllLines($inputFull, $Utf8NoBom)

$batchIndex = 1
$writtenFiles = New-Object System.Collections.Generic.List[string]
for ($offset = 0; $offset -lt $lines.Count; $offset += $BatchSize) {
    $count = [Math]::Min($BatchSize, $lines.Count - $offset)
    $batchLines = New-Object string[] $count
    [Array]::Copy($lines, $offset, $batchLines, 0, $count)
    $fileName = "batch_{0:D3}.jsonl" -f $batchIndex
    $outputPath = Join-Path $outputFull $fileName
    [System.IO.File]::WriteAllLines($outputPath, $batchLines, $Utf8NoBom)
    $writtenFiles.Add($outputPath)
    $batchIndex++
}

Write-Output "Input: $inputFull"
Write-Output "OutputDir: $outputFull"
Write-Output "Lines: $($lines.Count)"
Write-Output "BatchSize: $BatchSize"
Write-Output "Batches written: $($writtenFiles.Count)"
foreach ($file in $writtenFiles) {
    Write-Output "- $file"
}

