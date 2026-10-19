param(
    [Parameter(Mandatory = $true)]
    [string]$QmtPythonDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $QmtPythonDir -PathType Container)) {
    throw "QMT Python directory does not exist: $QmtPythonDir"
}

$files = @(
    @{ Source = 'large_order_limit_up_buy/QMT_LARGE_ORDER_LIMIT_UP_BUY.py'; Target = 'QMT_LARGE_ORDER_LIMIT_UP_BUY.py' },
    @{ Source = 'juejin_sell_strategy/QMT_JUEJIN_SELL.py'; Target = 'JUEJIN_SELL.py' }
)
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    foreach ($file in $files) {
        $source = Join-Path $PSScriptRoot $file.Source
        $target = Join-Path $QmtPythonDir $file.Target
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Source strategy does not exist: $source"
        }
        Copy-Item -LiteralPath $source -Destination $target -Force
        $sourceHash = [BitConverter]::ToString($sha256.ComputeHash([IO.File]::ReadAllBytes($source)))
        $targetHash = [BitConverter]::ToString($sha256.ComputeHash([IO.File]::ReadAllBytes($target)))
        if ($sourceHash -ne $targetHash) {
            throw "Strategy copy verification failed: $target"
        }
        Write-Output ("SYNCED {0} SHA256={1}" -f $target, $sourceHash.Replace('-', '').ToLowerInvariant())
    }
}
finally {
    $sha256.Dispose()
}
