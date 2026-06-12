[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,

    [Parameter(Mandatory = $true)]
    [string]$TranslatedPath,

    [string]$ErrorOutputPath = "qa/validation_errors.md"
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

function Test-LooksLikeUntranslatedEnglish {
    param([AllowNull()][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $false
    }
    return $Text -match "[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){4,}"
}

$sourceFull = Resolve-ProjectInputPath -PathValue $SourcePath -Name "SourcePath"
$translatedFull = Resolve-ProjectInputPath -PathValue $TranslatedPath -Name "TranslatedPath"
$errorFull = Resolve-ProjectOutputPath -PathValue $ErrorOutputPath -Name "ErrorOutputPath"

$errors = New-Object System.Collections.Generic.List[string]
$sourceLines = [System.IO.File]::ReadAllLines($sourceFull, $Utf8NoBom)
$translatedLines = [System.IO.File]::ReadAllLines($translatedFull, $Utf8NoBom)

if ($sourceLines.Count -ne $translatedLines.Count) {
    $errors.Add("Line count mismatch: source=$($sourceLines.Count), translated=$($translatedLines.Count)")
}

$maxLines = [Math]::Max($sourceLines.Count, $translatedLines.Count)
$identityFields = @("id", "plugin", "type", "field", "source")

for ($i = 0; $i -lt $maxLines; $i++) {
    $lineNumber = $i + 1
    if ($i -ge $sourceLines.Count) {
        $errors.Add("Line ${lineNumber}: missing source line")
        continue
    }
    if ($i -ge $translatedLines.Count) {
        $errors.Add("Line ${lineNumber}: missing translated line")
        continue
    }

    $sourceObject = $null
    $translatedObject = $null

    try {
        $sourceObject = $sourceLines[$i] | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        $errors.Add("Line ${lineNumber}: source is not valid JSON: $($_.Exception.Message)")
    }

    try {
        $translatedObject = $translatedLines[$i] | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        $errors.Add("Line ${lineNumber}: translated is not valid JSON: $($_.Exception.Message)")
    }

    if ($null -eq $sourceObject -or $null -eq $translatedObject) {
        continue
    }

    foreach ($fieldName in $identityFields) {
        $sourceValue = Get-JsonValue -ObjectValue $sourceObject -PropertyName $fieldName
        $translatedValue = Get-JsonValue -ObjectValue $translatedObject -PropertyName $fieldName
        if ([string]$sourceValue -cne [string]$translatedValue) {
            $errors.Add("Line ${lineNumber}: field '$fieldName' was modified. source='$sourceValue' translated='$translatedValue'")
        }
    }

    $target = [string](Get-JsonValue -ObjectValue $translatedObject -PropertyName "target")
    if ([string]::IsNullOrWhiteSpace($target)) {
        $errors.Add("Line ${lineNumber}: target is empty")
    }

    $sourceText = [string](Get-JsonValue -ObjectValue $sourceObject -PropertyName "source")
    $sourceTokens = @(Get-PlaceholderTokens -Text $sourceText)
    $targetTokens = @(Get-PlaceholderTokens -Text $target)
    foreach ($token in @($sourceTokens | Select-Object -Unique)) {
        $sourceCount = Get-TokenCount -Tokens $sourceTokens -Needle $token
        $targetCount = Get-TokenCount -Tokens $targetTokens -Needle $token
        if ($targetCount -lt $sourceCount) {
            $errors.Add("Line ${lineNumber}: placeholder missing from target: $token")
        }
    }

    if (Test-LooksLikeUntranslatedEnglish -Text $target) {
        $errors.Add("Line ${lineNumber}: target appears to contain an untranslated English long sentence")
    }
}

$report = New-Object System.Collections.Generic.List[string]
$report.Add("# Validation Errors")
$report.Add("")
$report.Add("- Source: $sourceFull")
$report.Add("- Translated: $translatedFull")
$report.Add("- Checked at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$report.Add("")

if ($errors.Count -eq 0) {
    $report.Add("当前暂无校验错误。")
    Write-Output "Validation passed: no errors."
}
else {
    foreach ($entry in $errors) {
        $message = "- $entry"
        $report.Add($message)
        Write-Output $message
    }
}

[System.IO.File]::WriteAllLines($errorFull, $report.ToArray(), $Utf8NoBom)
Write-Output "Validation report written to: $errorFull"

if ($errors.Count -gt 0) {
    exit 1
}
