[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Start', 'Status', 'Remove', 'Probe')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [Parameter(Mandatory = $false)]
    [string]$Python,
    [Parameter(Mandatory = $false)]
    [string]$ServiceEntry,
    [Parameter(Mandatory = $false)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $false)]
    [string]$WorkingDirectory
)

$ErrorActionPreference = 'Stop'

function Write-Result([hashtable]$Value) {
    $Value | ConvertTo-Json -Compress -Depth 4
}

try {
    if ($TaskName -notmatch '^Hermes_DocumentReader_[0-9a-f]{12}$') {
        throw 'invalid profile-scoped Document Reader task name'
    }
    if ($Action -eq 'Probe') {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Result @{
            ok = $true
            exists = ($null -ne $task)
            state = $(if ($null -eq $task) { 'Absent' } else { [string]$task.State })
        }
        exit 0
    }
    if ([string]::IsNullOrWhiteSpace($Python) -or
        [string]::IsNullOrWhiteSpace($ServiceEntry) -or
        [string]::IsNullOrWhiteSpace($ConfigPath) -or
        [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        throw 'exact scheduled task paths are required for this action'
    }
    $pythonPath = [IO.Path]::GetFullPath($Python)
    $entryPath = [IO.Path]::GetFullPath($ServiceEntry)
    $configFile = [IO.Path]::GetFullPath($ConfigPath)
    $workingPath = [IO.Path]::GetFullPath($WorkingDirectory)
    if (-not [IO.Path]::IsPathRooted($Python) -or
        -not [IO.Path]::IsPathRooted($ServiceEntry) -or
        -not [IO.Path]::IsPathRooted($ConfigPath) -or
        -not [IO.Path]::IsPathRooted($WorkingDirectory)) {
        throw 'all scheduled task paths must be absolute'
    }
    $expectedArguments = '-B -I -S -u "' + $entryPath + '" --config "' + $configFile + '"'
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $matches = $false
    if ($null -ne $task) {
        $actions = @($task.Actions)
        $matches = (
            $actions.Count -eq 1 -and
            [string]::Equals([IO.Path]::GetFullPath($actions[0].Execute), $pythonPath, [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals([string]$actions[0].Arguments, $expectedArguments, [StringComparison]::Ordinal) -and
            -not [string]::IsNullOrWhiteSpace([string]$actions[0].WorkingDirectory) -and
            [string]::Equals([IO.Path]::GetFullPath($actions[0].WorkingDirectory), $workingPath, [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals([string]$task.Principal.UserId, $currentUser, [StringComparison]::OrdinalIgnoreCase) -and
            [string]$task.Principal.RunLevel -eq 'Limited'
        )
    }

    if ($Action -eq 'Status') {
        Write-Result @{
            ok = $true
            exists = ($null -ne $task)
            action_matches = $(if ($null -eq $task) { $true } else { $matches })
            state = $(if ($null -eq $task) { 'Absent' } else { [string]$task.State })
        }
        exit 0
    }

    if ($null -ne $task -and -not $matches) {
        throw 'scheduled task exists but its exact action/principal is foreign'
    }

    if ($Action -eq 'Install') {
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $entryPath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $configFile -PathType Leaf) -or
            -not (Test-Path -LiteralPath $workingPath -PathType Container)) {
            throw 'scheduled service files are incomplete'
        }
        if ($null -eq $task) {
            $taskAction = New-ScheduledTaskAction -Execute $pythonPath -Argument $expectedArguments -WorkingDirectory $workingPath
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
            $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        }
    }
    elseif ($Action -eq 'Start') {
        if ($null -eq $task) { throw 'owned scheduled task is missing' }
        Start-ScheduledTask -TaskName $TaskName
    }
    elseif ($Action -eq 'Remove') {
        if ($null -ne $task) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    Write-Result @{ ok = $true }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
