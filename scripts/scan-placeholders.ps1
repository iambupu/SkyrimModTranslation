[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [string]$ReportOutputPath = "qa/placeholder_report.md"
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

function Resolve-ProjectOutputPath {
    param([string]$PathValue, [string]$Name)
    $candidate = if ([System.IO.Path]::IsPathRooted($PathValue)) { $PathValue } else { Join-Path $script:ProjectRoot $PathValue }
    $full = [System.IO.Path]::GetFullPath($candidate)
    if (-not (Test-InProjectRoot $full)) {
        throw "$Name must be inside project root: $PathValue"
    }
    $parent = Split-Path -Parent $full
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    $resolvedParent = (Resolve-Path -LiteralPath $parent).ProviderPath
    if (-not (Test-InProjectRoot $resolvedParent)) {
        throw "$Name parent must be inside project root: $PathValue"
    }
    return $full
}

function Get-JsonValue {
    param([object]$ObjectValue, [string]$PropertyName)
    $property = $ObjectValue.PSObject.Properties[$PropertyName]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-PlaceholderTokens {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) {
        return @()
    }

    $patterns = @(
        '%[sdf]',
        '\{(?:0|1|name)\}',
        '<[^>\r\n]+>',
        '\$[\p{L}_][\p{L}\p{N}_]*',
        '\\r\\n',
        '\\n'
    )

    $tokens = New-Object System.Collections.Generic.List[string]
    foreach ($pattern in $patterns) {
        foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
            $tokens.Add($match.Value)
        }
    }
    return $tokens.ToArray()
}

function Get-TokenCount {
    param([string[]]$Tokens, [string]$Needle)
    return @($Tokens | Where-Object { $_ -ceq $Needle }).Count
}

$inputFull = Resolve-ProjectInputPath -PathValue $InputPath -Name "InputPath"
$reportFull = Resolve-ProjectOutputPath -PathValue $ReportOutputPath -Name "ReportOutputPath"

$inputItem = Get-Item -LiteralPath $inputFull
if ($inputItem.PSIsContainer) {
    $jsonlFiles = @(Get-ChildItem -LiteralPath $inputFull -Recurse -File -Filter "*.jsonl")
}
else {
    $jsonlFiles = @($inputItem)
}

$issues = New-Object System.Collections.Generic.List[string]
$checkedRows = 0

foreach ($file in $jsonlFiles) {
    $lines = [System.IO.File]::ReadAllLines($file.FullName, $Utf8NoBom)
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $lineNumber = $i + 1
        try {
            $object = $lines[$i] | ConvertFrom-Json -ErrorAction Stop
            $checkedRows++
        }
        catch {
            $issues.Add("$($file.FullName):$lineNumber invalid JSON: $($_.Exception.Message)")
            continue
        }

        $source = [string](Get-JsonValue -ObjectValue $object -PropertyName "source")
        $target = [string](Get-JsonValue -ObjectValue $object -PropertyName "target")
        $sourceTokens = @(Get-PlaceholderTokens -Text $source)
        $targetTokens = @(Get-PlaceholderTokens -Text $target)
        $allTokens = @($sourceTokens + $targetTokens | Select-Object -Unique)
        foreach ($token in $allTokens) {
            $sourceCount = Get-TokenCount -Tokens $sourceTokens -Needle $token
            $targetCount = Get-TokenCount -Tokens $targetTokens -Needle $token
            if ($sourceCount -ne $targetCount) {
                $issues.Add("$($file.FullName):$lineNumber placeholder mismatch '$token' source=$sourceCount target=$targetCount")
            }
        }
    }
}

$report = New-Object System.Collections.Generic.List[string]
$report.Add("# Placeholder Report")
$report.Add("")
$report.Add("- Input: $inputFull")
$report.Add("- Files checked: $($jsonlFiles.Count)")
$report.Add("- Rows checked: $checkedRows")
$report.Add("- Checked at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$report.Add("")

if ($issues.Count -eq 0) {
    $report.Add("当前暂无占位符差异。")
    Write-Output "Placeholder scan passed: no issues."
}
else {
    foreach ($issue in $issues) {
        $line = "- $issue"
        $report.Add($line)
        Write-Output $line
    }
}

[System.IO.File]::WriteAllLines($reportFull, $report.ToArray(), $Utf8NoBom)
Write-Output "Placeholder report written to: $reportFull"

if ($issues.Count -gt 0) {
    exit 1
}

