
# ============================================================
# MEDA Derived Products Downloader
# NASA PDS - Sols 230 to 239
# Products: ANC, PS, RHS, TIRS, WS
# ============================================================

$ErrorActionPreference = "Stop"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$BaseIndex = "https://pds-atmospheres.nmsu.edu/data_and_services/atmospheres_data/PERSEVERANCE/logs"

$DownloadEndpoint = "https://atmos.nmsu.edu:8443/data_searches/Download_URN"

$StartSol = 230
$EndSol = 239

$OutputRoot = Join-Path (Get-Location) "resources\MEDA_Weather_ds\derived"

$Products = @{
    "ANC"  = "MARS2020_MEDA_Ancillary_Derived_Del11.htm"
    "PS"   = "MARS2020_MEDA_PS_Derived_Del11.htm"
    "RHS"  = "MARS2020_MEDA_RHS_Derived_Del11.htm"
    "TIRS" = "MARS2020_MEDA_TIRS_Derived_Del11.htm"
    "WS"   = "MARS2020_MEDA_WS_Derived_Del11.htm"
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ------------------------------------------------------------
# Extract URNs from a PDS HTML index
# ------------------------------------------------------------

function Get-ProductRecords {
    param(
        [string]$Html,
        [string]$Product
    )

    $records = @()

    $rows = [regex]::Matches(
        $Html,
        '(?is)<tr\b[^>]*>(.*?)</tr>'
    )

    foreach ($row in $rows) {

        $cells = [regex]::Matches(
            $row.Groups[1].Value,
            '(?is)<td\b[^>]*>(.*?)</td>'
        )

        if ($cells.Count -lt 7) {
            continue
        }

        $values = @()

        foreach ($cell in $cells) {
            $value = [regex]::Replace(
                $cell.Groups[1].Value,
                '(?is)<[^>]+>',
                ''
            )

            $values += [System.Net.WebUtility]::HtmlDecode(
                $value
            ).Trim()
        }

        $urn = $values[1]

        if ($urn -notmatch '^urn:nasa:pds:') {
            continue
        }

        $start = 0
        $end = 0

        if (
            -not [int]::TryParse($values[5], [ref]$start) -or
            -not [int]::TryParse($values[6], [ref]$end)
        ) {
            continue
        }

        if ($start -gt $EndSol -or $end -lt $StartSol) {
            continue
        }

        $records += [PSCustomObject]@{
            Product  = $Product
            URN      = $urn
            StartSol = $start
            EndSol   = $end
        }
    }

    return $records
}

# ------------------------------------------------------------
# Download one product using the verified NASA POST endpoint
# ------------------------------------------------------------

function Download-Product {
    param(
        [string]$Product,
        [string]$URN
    )

    $ProductDirectory = Join-Path $OutputRoot $Product

    New-Item -ItemType Directory -Force -Path $ProductDirectory |
        Out-Null

    Write-Host "`n[$Product] $URN" -ForegroundColor Cyan

    try {

        $response = Invoke-WebRequest `
            -Uri $DownloadEndpoint `
            -Method POST `
            -Body @{ file_name = $URN } `
            -ContentType "application/x-www-form-urlencoded" `
            -UseBasicParsing

        $contentType = [string]$response.Headers["Content-Type"]

        if ($contentType -notmatch "application/zip") {
            throw "Unexpected response Content-Type: $contentType"
        }

        [byte[]]$bytes = $response.Content

        if (
            $bytes.Length -lt 4 -or
            $bytes[0] -ne 0x50 -or
            $bytes[1] -ne 0x4B
        ) {
            throw "Response does not have a valid ZIP signature."
        }

        $tempZip = Join-Path $env:TEMP (
            "MEDA_" + [guid]::NewGuid().ToString() + ".zip"
        )

        [System.IO.File]::WriteAllBytes($tempZip, $bytes)

        $archive = [System.IO.Compression.ZipFile]::OpenRead($tempZip)

        try {

            if ($archive.Entries.Count -eq 0) {
                throw "ZIP archive is empty."
            }

            foreach ($entry in $archive.Entries) {

                if ([string]::IsNullOrWhiteSpace($entry.Name)) {
                    continue
                }

                # Prevent archive path traversal
                $fileName = [System.IO.Path]::GetFileName($entry.Name)

                if ([string]::IsNullOrWhiteSpace($fileName)) {
                    continue
                }

                $destination = Join-Path $ProductDirectory $fileName

                if (Test-Path $destination) {
                    Write-Host "  Already exists: $fileName" -ForegroundColor Yellow
                    continue
                }

                $inputStream = $entry.Open()

                try {
                    $outputStream = [System.IO.File]::Create($destination)

                    try {
                        $inputStream.CopyTo($outputStream)
                    }
                    finally {
                        $outputStream.Dispose()
                    }
                }
                finally {
                    $inputStream.Dispose()
                }

                if (
                    -not (Test-Path $destination) -or
                    (Get-Item $destination).Length -eq 0
                ) {
                    throw "Extracted file is missing or empty: $fileName"
                }

                Write-Host "  Extracted: $fileName" -ForegroundColor Green
            }
        }
        finally {
            $archive.Dispose()
            Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
        }

        return $true
    }
    catch {
        Write-Host "  FAILED: $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

# ============================================================
# MAIN
# ============================================================

$allRecords = @()

Write-Host "`n========================================"
Write-Host "MEDA DERIVED INDEX DISCOVERY"
Write-Host "Sols $StartSol - $EndSol"
Write-Host "========================================"

foreach ($product in $Products.Keys) {

    $indexUrl = "$BaseIndex/$($Products[$product])"

    Write-Host "`nReading index: $product"

    try {

        $indexResponse = Invoke-WebRequest `
            -Uri $indexUrl `
            -UseBasicParsing

        if ($indexResponse.StatusCode -ne 200) {
            throw "Index HTTP status: $($indexResponse.StatusCode)"
        }

        $records = @(
            Get-ProductRecords `
                -Html $indexResponse.Content `
                -Product $product
        )

        Write-Host "  Matching records: $($records.Count)"

        $allRecords += $records
    }
    catch {
        Write-Host "  INDEX ERROR: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# Remove duplicate URNs
$allRecords = @(
    $allRecords |
        Sort-Object URN -Unique
)

Write-Host "`n========================================"
Write-Host "DOWNLOAD"
Write-Host "========================================"

$downloaded = 0
$failed = 0

foreach ($record in $allRecords) {

    $ok = Download-Product `
        -Product $record.Product `
        -URN $record.URN

    if ($ok) {
        $downloaded++
    }
    else {
        $failed++
    }

    # Avoid sending requests too quickly
    Start-Sleep -Milliseconds 500
}

# ============================================================
# FINAL REPORT
# ============================================================

Write-Host "`n========================================"
Write-Host "DOWNLOAD SUMMARY"
Write-Host "========================================"

Write-Host "Discovered : $($allRecords.Count)"
Write-Host "Successful : $downloaded"
Write-Host "Failed     : $failed"

Write-Host "`nOutput directory:"
Write-Host $OutputRoot

Write-Host "`n========================================"
Write-Host "FILES ON DISK"
Write-Host "========================================"

foreach ($product in $Products.Keys) {

    $folder = Join-Path $OutputRoot $product

    if (Test-Path $folder) {

        $files = @(Get-ChildItem $folder -File)

        $totalBytes = (
            $files |
                Measure-Object -Property Length -Sum
        ).Sum

        if ($null -eq $totalBytes) {
            $totalBytes = 0
        }

        Write-Host (
            "{0,-6} Files: {1,-4} Size: {2:N2} MB" -f
            $product,
            $files.Count,
            ($totalBytes / 1MB)
        )
    }
    else {
        Write-Host "$product : No directory created"
    }
}

Write-Host "`nFinished."
