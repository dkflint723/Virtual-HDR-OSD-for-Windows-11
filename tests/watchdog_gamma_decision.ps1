# Exercises Get-DesiredExtendedProfile from the shipped watchdog in isolation.
#
# The function decides which HDR profile Windows should be associated with, and a wrong
# answer silently reverts the user's correction setting. It cannot be reached without
# installing a scheduled task, so the test harness extracts it and stubs the native layer.
#
# Nothing here touches a real colour profile, association, or scheduled task.

param([Parameter(Mandatory = $true)][string]$FunctionsPath)

$ErrorActionPreference = 'Stop'   # matches the watchdog itself
$script:fail = 0

$colorDir = Join-Path ([System.IO.Path]::GetTempPath()) ('vhdrosd-wd-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $colorDir | Out-Null
foreach ($n in 'VOff.icm', 'VOn.icm', 'NewOff.icm', 'NewOn.icm') {
    Set-Content -LiteralPath (Join-Path $colorDir $n) -Value 'stub'
}

# Stub for the native helper the real script defines over mscms.
function Test-InstalledColorProfile {
    param([string]$ProfileName)
    if ([string]::IsNullOrWhiteSpace($ProfileName)) { return $false }
    return Test-Path -LiteralPath (Join-Path $colorDir $ProfileName)
}

$script:GammaStatePath = Join-Path ([System.IO.Path]::GetTempPath()) ('vhdrosd-wd-gamma-' + [guid]::NewGuid().ToString('N') + '.json')
. $FunctionsPath

function Set-Runtime {
    param($enabled, $updatedAt, $profiles = @{ Off = 'VOff.icm'; On = 'VOn.icm' }, $active = 'VOn.icm')
    @{ displays = @{ d1 = @{
        gdi_name = '\\.\DISPLAY1'; enabled = $enabled; updated_at = $updatedAt
        active_profile = $active; profiles = $profiles } } } |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:GammaStatePath -Encoding UTF8
}

function New-Saved {
    param($gammaEnabled, $updatedAt)
    $o = [pscustomobject]@{
        GdiName = '\\.\DISPLAY1'; StandardProfile = 'sRGB.icm'; ExtendedProfile = 'Base.icm'
        WorkingOff = 'VOff.icm'; WorkingOn = 'VOn.icm'; GammaEnabled = $gammaEnabled
    }
    if ($null -ne $updatedAt) {
        $o | Add-Member -NotePropertyName GammaUpdatedAt -NotePropertyValue $updatedAt -Force
    }
    return $o
}

$display = [pscustomobject]@{ GdiName = '\\.\DISPLAY1' }

function Assert-Profile {
    param($Name, $Expected, $Actual)
    if ($Expected -eq $Actual) {
        Write-Host "  PASS  $Name"
    } else {
        $script:fail++
        Write-Host "  FAIL  $Name (expected=$Expected actual=$Actual)"
    }
}

$old = (Get-Date).AddMinutes(-10).ToString('o')
$new = (Get-Date).ToString('o')

# The regression this function exists to prevent.
Set-Runtime -enabled $false -updatedAt $new -active 'VOff.icm'
Assert-Profile 'GUI turned correction off and is newer -> Off wins' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# The watchdog must still win when it is the one that acted last.
Set-Runtime -enabled $true -updatedAt $old
Assert-Profile 'watchdog hotkey is newer -> its own choice wins' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $new))

# State.json written before GammaUpdatedAt existed.
Set-Runtime -enabled $true -updatedAt $new
Assert-Profile 'no GammaUpdatedAt -> GUI intent wins' 'VOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $null))

# GUI closed / never ran.
Remove-Item -LiteralPath $script:GammaStatePath -Force
Assert-Profile 'no runtime file, correction off -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))
Assert-Profile 'no runtime file, correction on -> captured state' 'VOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# Malformed input must never throw under $ErrorActionPreference = 'Stop'.
Set-Content -LiteralPath $script:GammaStatePath -Value '{ not json' -Encoding UTF8
Assert-Profile 'corrupt runtime json -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))
Set-Runtime -enabled $true -updatedAt 'not-a-timestamp'
Assert-Profile 'unparseable timestamp -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))
Set-Runtime -enabled $true -updatedAt ''
Assert-Profile 'empty timestamp -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))

# Never hand Windows a profile that is not installed.
Set-Runtime -enabled $true -updatedAt $new -profiles @{ Off = 'Ghost_Off.icm'; On = 'Ghost_On.icm' }
Assert-Profile 'GUI names an uninstalled profile -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))

# The GUI regenerates the pair under new filenames when the adapter LUID changes.
Set-Runtime -enabled $true -updatedAt $new -profiles @{ Off = 'NewOff.icm'; On = 'NewOn.icm' }
Assert-Profile 'adapter LUID changed -> GUI filenames preferred' 'NewOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))

# The GUI wrote naive local timestamps before they became offset-aware.
Set-Runtime -enabled $false -updatedAt (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss') -active 'VOff.icm'
Assert-Profile 'legacy naive timestamp still compares' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

Remove-Item -LiteralPath $script:GammaStatePath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $colorDir -Recurse -Force -ErrorAction SilentlyContinue

if ($script:fail -eq 0) { Write-Host 'ALL PASS' } else { Write-Host "$($script:fail) FAILURE(S)" }
exit $script:fail
