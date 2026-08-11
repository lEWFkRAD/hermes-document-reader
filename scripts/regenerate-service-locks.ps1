[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$Python311,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$Python314,

    [string]$Uv = "uv"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$serviceRequirements = Join-Path $repositoryRoot "install/service-requirements.txt"
$bootstrapRequirements = Join-Path $PSScriptRoot "lock-inputs/service-bootstrap.txt"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$uvVersion = (& $Uv --version).Trim()
if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch '^uv 0\.12\.3(?:\s|$)') {
    throw "Service locks require uv 0.12.3; found '$uvVersion'"
}

$targets = @(
    [pscustomobject]@{
        Python = $Python311
        Minor = "3.11"
        Target = "windows-cpython-311-x86_64"
        Output = Join-Path $repositoryRoot "install/locks/windows-cpython-311-x86_64.txt"
    },
    [pscustomobject]@{
        Python = $Python314
        Minor = "3.14"
        Target = "windows-cpython-314-x86_64"
        Output = Join-Path $repositoryRoot "install/locks/windows-cpython-314-x86_64.txt"
    }
)

$generated = @()
try {
    foreach ($target in $targets) {
        $identity = (& $target.Python -I -c "import platform,sys; print(f'{sys.implementation.name}|{sys.version_info.major}.{sys.version_info.minor}|{sys.platform}|{platform.machine().lower()}')").Trim()
        $expectedIdentity = "cpython|$($target.Minor)|win32|amd64"
        if ($LASTEXITCODE -ne 0 -or $identity -notin @($expectedIdentity, $expectedIdentity.Replace("amd64", "x86_64"))) {
            throw "$($target.Target) requires $expectedIdentity; found '$identity'"
        }

        $body = [System.IO.Path]::GetTempFileName()
        $generated += $body
        & $Uv pip compile `
            $serviceRequirements `
            $bootstrapRequirements `
            --python $target.Python `
            --python-platform x86_64-pc-windows-msvc `
            --only-binary ":all:" `
            --generate-hashes `
            --no-annotate `
            --no-header `
            --no-cache `
            --output-file $body
        if ($LASTEXITCODE -ne 0) {
            throw "uv failed to resolve $($target.Target)"
        }

        $bodyText = [System.IO.File]::ReadAllText($body).Replace("`r`n", "`n").Replace("`r", "`n").Trim()
        $header = @(
            "# Hermes Document Reader service dependency lock"
            "# target: $($target.Target)"
            "# sources: install/service-requirements.txt + scripts/lock-inputs/service-bootstrap.txt"
            "# generator: uv 0.12.3; uv pip compile --generate-hashes --only-binary=:all:"
            "# install: python -m pip install --require-hashes --only-binary=:all: --requirement <this-file>"
        ) -join "`n"
        [System.IO.File]::WriteAllText($target.Output, "$header`n$bodyText`n", $utf8NoBom)
    }

    & node (Join-Path $PSScriptRoot "validate-service-locks.mjs")
    if ($LASTEXITCODE -ne 0) {
        throw "Generated service locks failed repository validation"
    }
} finally {
    foreach ($temporaryFile in $generated) {
        Remove-Item -LiteralPath $temporaryFile -Force -ErrorAction SilentlyContinue
    }
}
