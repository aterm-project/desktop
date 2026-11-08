#requires -Version 7.4
<#
Atermius Windows runtime build (GitHub-hosted Windows runner, pwsh 7.4+).

Inputs are hashes in environment variables, set by the workflow from
workflow_dispatch inputs. Private assets must already be in -InputsDir:
  source.tar.gz  source-manifest.json  native-bundle.zip

The script never prints file contents, tokens or environment dumps. Every
verification fails closed. Archive and manifest policy lives in winrt_tool.py
so the same code is self-tested on Linux by root.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputsDir,
    [Parameter(Mandatory)][string]$WorkRoot,
    [Parameter(Mandatory)][string]$OutDir
)
Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$ProgressPreference = 'SilentlyContinue'

$Tool = Join-Path $PSScriptRoot 'winrt_tool.py'
$script:Stage = 'init'
$script:Started = [DateTimeOffset]::UtcNow
$Result = [ordered]@{
    schema = 'atermius-windows-build-result/1'; status = 'failed'; stage = $null
    started_at = $script:Started.ToString('o'); finished_at = $null; stage_seconds = [ordered]@{}
}
$script:StageClock = [Diagnostics.Stopwatch]::StartNew()

function Enter-Stage([string]$Name) {
    if ($script:Stage -ne 'init') { $Result.stage_seconds[$script:Stage] = [math]::Round($script:StageClock.Elapsed.TotalSeconds, 1) }
    $script:Stage = $Name; $script:StageClock.Restart()
    Write-Host "::group::$Name"
}
function Exit-Group { Write-Host '::endgroup::' }

function Get-Hex64([string]$Name, [switch]$Optional) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($v)) {
        if ($Optional) { return '' }
        throw "Missing required environment value $Name."
    }
    $v = $v.Trim().ToLowerInvariant()
    if ($v -notmatch '^[0-9a-f]{64}$') { throw "$Name must be 64 lowercase hex characters." }
    return $v
}
function Get-Required([string]$Name, [string]$Pattern) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($v) -or $v -notmatch $Pattern) { throw "Environment value $Name is missing or malformed." }
    return $v.Trim()
}
function Get-Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function ConvertTo-CMakePath([string]$Path) { ([IO.Path]::GetFullPath($Path)) -replace '\\', '/' }

function Invoke-Tool {
    # Deliberately a simple (non-advanced) function: '--flag' tokens must reach
    # Python verbatim via $args instead of being bound as PowerShell parameters.
    $out = & $script:Python -I -X utf8 $Tool @args | Out-String
    return ($out | ConvertFrom-Json)
}

function Assert-EmptyDir([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        if (@(Get-ChildItem -LiteralPath $Path -Force).Count -ne 0) { throw "Work directory is not empty: $Path" }
    } else { New-Item -ItemType Directory -Path $Path | Out-Null }
}

function Expand-TrustedZip([string]$Zip, [string]$Dest) {
    # Only used for archives whose SHA-256 was verified just before. The .NET 8
    # extractor rejects entries resolving outside $Dest.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
}

try {
    # ------------------------------------------------------------------ inputs
    Enter-Stage 'validate-inputs'
    $SourceSha = Get-Hex64 'ATERMIUS_SOURCE_SHA256'
    $ManifestSha = Get-Hex64 'ATERMIUS_SOURCE_MANIFEST_SHA256'
    $NativeSha = Get-Hex64 'ATERMIUS_NATIVE_BUNDLE_SHA256'
    $EngineSha = Get-Hex64 'ATERMIUS_ENGINE_SHA256'
    $FirebaseSha = Get-Hex64 'ATERMIUS_FIREBASE_SDK_SHA256' -Optional
    $FlutterVersion = Get-Required 'FLUTTER_VERSION' '^\d+\.\d+\.\d+$'
    $FlutterSdkSha = Get-Hex64 'FLUTTER_SDK_SHA256'
    $FlutterRevision = Get-Required 'FLUTTER_REVISION' '^[0-9a-f]{40}$'
    $DartVersion = Get-Required 'DART_VERSION' '^\d+\.\d+\.\d+$'
    $Workers = [int](Get-Required 'ATERMIUS_MAX_WORKERS' '^[1-8]$')
    $InputTag = Get-Required 'ATERMIUS_INPUT_TAG' '^ci-input-[A-Za-z0-9._-]{1,80}$'
    foreach ($n in 'source.tar.gz', 'source-manifest.json', 'native-bundle.zip') {
        if (!(Test-Path -LiteralPath (Join-Path $InputsDir $n) -PathType Leaf)) { throw "Missing input asset $n." }
    }
    $extra = @(Get-ChildItem -LiteralPath $InputsDir -Force | Where-Object { $_.Name -notin 'source.tar.gz', 'source-manifest.json', 'native-bundle.zip' })
    if ($extra.Count) { throw "Unexpected files among downloaded inputs: $($extra.Name -join ', ')" }
    $WorkRoot = [IO.Path]::GetFullPath($WorkRoot); $OutDir = [IO.Path]::GetFullPath($OutDir)
    Assert-EmptyDir $WorkRoot; Assert-EmptyDir $OutDir
    $Src = Join-Path $WorkRoot 'src'; $Native = Join-Path $WorkRoot 'native'
    $App = Join-Path $Src 'apps\client'; $Meta = Join-Path $OutDir 'meta'
    New-Item -ItemType Directory -Path $Meta | Out-Null

    $script:Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
    $pyVersion = (& $script:Python -I -c 'import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version.split()[0])').Trim()
    $Result.inputs = [ordered]@{
        input_release_tag = $InputTag; source_sha256 = $SourceSha; source_manifest_sha256 = $ManifestSha
        native_bundle_sha256 = $NativeSha; engine_sha256 = $EngineSha
        firebase_sdk_sha256 = $(if ($FirebaseSha) { $FirebaseSha } else { $null })
    }
    $Result.environment = [ordered]@{
        image_os = $env:ImageOS; image_version = $env:ImageVersion; runner_os = $env:RUNNER_OS
        runner_arch = $env:RUNNER_ARCH; processors = $env:NUMBER_OF_PROCESSORS; python = $pyVersion
        pwsh = $PSVersionTable.PSVersion.ToString(); max_workers = $Workers
        ci_commit = $env:GITHUB_SHA; ci_ref = $env:GITHUB_REF; run_id = $env:GITHUB_RUN_ID; run_attempt = $env:GITHUB_RUN_ATTEMPT
    }
    Exit-Group

    # ------------------------------------------------------------------ verified inputs
    Enter-Stage 'extract-inputs'
    $Result.native = Invoke-Tool extract-native --zip (Join-Path $InputsDir 'native-bundle.zip') `
        --expect-sha $NativeSha --expect-engine-sha $EngineSha --dest $Native `
        --manifest-out (Join-Path $Meta 'NATIVE_BUNDLE_MANIFEST.json')
    $Result.source = Invoke-Tool extract-source --tar (Join-Path $InputsDir 'source.tar.gz') `
        --manifest (Join-Path $InputsDir 'source-manifest.json') `
        --expect-tar-sha $SourceSha --expect-manifest-sha $ManifestSha --dest $Src
    Copy-Item -LiteralPath (Join-Path $InputsDir 'source-manifest.json') -Destination (Join-Path $Meta 'source-manifest.json')
    Exit-Group

    # ------------------------------------------------------------------ toolchain
    # Fail fast (before multi-GB downloads) if the runner image lacks C++ x64 + ATL.
    Enter-Stage 'visual-studio'
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = @(& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 Microsoft.VisualStudio.Component.VC.ATL -format json | Out-String | ConvertFrom-Json)
    if ($vs.Count -ne 1 -or -not $vs[0].installationPath) { throw 'No runner Visual Studio instance with C++ x64 tools and ATL (Microsoft.VisualStudio.Component.VC.ATL).' }
    $atl = @(Get-ChildItem -Path (Join-Path $vs[0].installationPath 'VC\Tools\MSVC\*\atlmfc\include\atlstr.h') -ErrorAction SilentlyContinue)
    if ($atl.Count -eq 0) { throw 'Runner Visual Studio lacks ATL headers (atlstr.h), required by flutter_secure_storage_windows.' }
    $Result.visual_studio = [ordered]@{ display = $vs[0].displayName; version = $vs[0].installationVersion; atl_toolsets = @($atl | ForEach-Object { $_.Directory.Parent.Parent.Name }) }
    Exit-Group

    Enter-Stage 'flutter-sdk'
    $base = 'https://storage.googleapis.com/flutter_infra_release/releases'
    $archiveRel = "stable/windows/flutter_windows_$FlutterVersion-stable.zip"
    $sdkZip = Join-Path $WorkRoot 'flutter-sdk.zip'
    Invoke-WebRequest -Uri "$base/$archiveRel" -OutFile $sdkZip -MaximumRetryCount 3 -RetryIntervalSec 15
    if ((Get-Sha256 $sdkZip) -ne $FlutterSdkSha) { throw 'Flutter SDK archive SHA-256 differs from the pinned value.' }
    # Second, independent check against the official release index.
    $index = Invoke-RestMethod -Uri "$base/releases_windows.json" -MaximumRetryCount 3 -RetryIntervalSec 15
    $entry = @($index.releases | Where-Object { $_.archive -eq $archiveRel })
    if ($entry.Count -ne 1 -or $entry[0].sha256 -ne $FlutterSdkSha -or $entry[0].hash -ne $FlutterRevision -or
        $entry[0].channel -ne 'stable' -or $entry[0].dart_sdk_version -ne $DartVersion) {
        throw 'Official releases_windows.json does not confirm the pinned Flutter SDK.'
    }
    $SdkRoot = Join-Path $WorkRoot 'sdk'
    Expand-TrustedZip $sdkZip $SdkRoot
    Remove-Item -LiteralPath $sdkZip
    $FlutterBin = Join-Path $SdkRoot 'flutter\bin'
    $env:PATH = "$FlutterBin;$env:PATH"
    $env:PUB_CACHE = Join-Path $WorkRoot 'pub'
    $env:FLUTTER_SUPPRESS_ANALYTICS = 'true'
    $env:CI = 'true'
    flutter config --no-analytics | Out-Null
    dart --disable-analytics | Out-Null
    $verText = (flutter --version --machine | Out-String)
    $ver = $verText.Substring($verText.IndexOf('{')) | ConvertFrom-Json
    if ($ver.frameworkVersion -ne $FlutterVersion -or $ver.frameworkRevision -ne $FlutterRevision -or
        $ver.channel -ne 'stable' -or -not ($ver.dartSdkVersion -eq $DartVersion -or $ver.dartSdkVersion.StartsWith("$DartVersion "))) {
        throw "Flutter toolchain mismatch: $($ver.frameworkVersion)/$($ver.frameworkRevision)/$($ver.dartSdkVersion)"
    }
    $Result.flutter = [ordered]@{ version = $ver.frameworkVersion; revision = $ver.frameworkRevision; dart = $ver.dartSdkVersion; sdk_sha256 = $FlutterSdkSha; engine_revision = $ver.engineRevision }
    Exit-Group


    # ------------------------------------------------------------------ dependencies
    Enter-Stage 'pub-get'
    Push-Location $App
    try { flutter pub get --enforce-lockfile } finally { Pop-Location }
    $Result.lock = Invoke-Tool check-lock --app-dir $App --root $Src
    Invoke-Tool reverify-source --manifest (Join-Path $Meta 'source-manifest.json') --root $Src --stage pub-get | Out-Null
    Exit-Group

    Enter-Stage 'firebase-sdk'
    $pcPath = [IO.Path]::GetFullPath((Join-Path $App '.dart_tool\package_config.json'))
    $pc = Get-Content -LiteralPath $pcPath -Raw | ConvertFrom-Json
    $fc = @($pc.packages | Where-Object { $_.name -eq 'firebase_core' })
    if ($fc.Count -eq 1) {
        # package_config.json allows rootUri to be relative to the config FILE, e.g. "../../../../pub/hosted/..." (WorkRoot\pub).
        # RFC 3986 resolution via .NET Uri(Uri base, string relative): an absolute rootUri such as file:///D:/...
        # is returned unchanged. LocalPath then percent-decodes. No string or path joining is involved.
        $pcUri = [Uri]::new($pcPath, [UriKind]::Absolute)
        $rootUri = [Uri]::new($pcUri, [string]$fc[0].rootUri)
        if (-not $rootUri.IsAbsoluteUri -or -not $rootUri.IsFile) { throw 'firebase_core rootUri does not resolve to a file URI.' }
        $fcRoot = [IO.Path]::GetFullPath($rootUri.LocalPath).TrimEnd('\')
        if (-not $fcRoot.StartsWith($WorkRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'firebase_core package root resolves outside the CI work root.'
        }
        if (!(Test-Path -LiteralPath (Join-Path $fcRoot 'windows\CMakeLists.txt') -PathType Leaf)) {
            throw 'Resolved firebase_core package root lacks windows\CMakeLists.txt.'
        }
        $cm = Get-Content -LiteralPath (Join-Path $fcRoot 'windows\CMakeLists.txt') -Raw
        if ($cm -notmatch 'set\(FIREBASE_SDK_VERSION "(?<v>\d+\.\d+\.\d+)"\)') { throw 'Cannot read FIREBASE_SDK_VERSION from firebase_core.' }
        $fbVersion = $Matches['v']
        $fbZip = Join-Path $WorkRoot "firebase_cpp_sdk_windows_$fbVersion.zip"
        Invoke-WebRequest -Uri "https://dl.google.com/firebase/sdk/cpp/firebase_cpp_sdk_windows_$fbVersion.zip" -OutFile $fbZip -MaximumRetryCount 3 -RetryIntervalSec 15
        $fbSha = Get-Sha256 $fbZip
        if ($FirebaseSha -and $fbSha -ne $FirebaseSha) { throw 'Firebase C++ SDK SHA-256 differs from the dispatch input.' }
        if (-not $FirebaseSha) { Write-Host "::warning::Firebase C++ SDK $fbVersion is not pinned by input; recorded sha256 $fbSha. Pin it next run." }
        $fbRoot = Join-Path $WorkRoot 'fb'
        Expand-TrustedZip $fbZip $fbRoot
        Remove-Item -LiteralPath $fbZip
        $fbDir = Join-Path $fbRoot 'firebase_cpp_sdk_windows'
        $vh = Get-Content -LiteralPath (Join-Path $fbDir 'include\firebase\version.h') -Raw
        $parts = foreach ($k in 'MAJOR', 'MINOR', 'REVISION') { if ($vh -match "FIREBASE_VERSION_$k (\d+)") { $Matches[1] } }
        if (($parts -join '.') -ne $fbVersion) { throw 'Extracted Firebase SDK version.h does not match the plugin version.' }
        # firebase_core's CMake uses this verified copy instead of its own unverified download.
        $env:FIREBASE_CPP_SDK_DIR = ConvertTo-CMakePath $fbDir
        $Result.firebase_sdk = [ordered]@{ version = $fbVersion; sha256 = $fbSha; pinned = [bool]$FirebaseSha; plugin = Split-Path $fcRoot -Leaf }
    } else { $Result.firebase_sdk = $null }
    Exit-Group

    # ------------------------------------------------------------------ build
    Enter-Stage 'flutter-build'
    # Bounded parallelism without touching the frozen tree: MSBuild picks up
    # Directory.Build.rsp from ancestors of the generated projects.
    Set-Content -LiteralPath (Join-Path $WorkRoot 'Directory.Build.rsp') -Encoding ascii -Value @("-maxCpuCount:$Workers", '-nodeReuse:false')
    $env:CMAKE_BUILD_PARALLEL_LEVEL = "$Workers"
    $env:UseMultiToolTask = 'true'
    $env:EnforceProcessCountAcrossBuilds = 'true'
    $env:CL_MPCount = "$Workers"
    $env:ATERMIUS_RDP_DIR = ConvertTo-CMakePath $Native
    Push-Location $App
    try { flutter build windows --release --no-pub } finally { Pop-Location }
    Invoke-Tool reverify-source --manifest (Join-Path $Meta 'source-manifest.json') --root $Src --stage build | Out-Null
    Exit-Group

    # ------------------------------------------------------------------ package + self-verify
    Enter-Stage 'package'
    $Release = Join-Path $App 'build\windows\x64\runner\Release'
    $zip = Join-Path $OutDir 'atermius-windows-x64.zip'
    $manifest = Join-Path $OutDir 'release-manifest.json'
    $metaArgs = @(
        "source_sha256=$SourceSha", "source_manifest_sha256=$ManifestSha", "native_bundle_sha256=$NativeSha",
        "pubspec_lock_sha256=$($Result.source.pubspec_lock_sha256)", "input_release_tag=$InputTag",
        "flutter_version=$FlutterVersion", "flutter_revision=$FlutterRevision", "dart_version=$DartVersion",
        "flutter_sdk_sha256=$FlutterSdkSha", "vs_version=$($Result.visual_studio.version)",
        "image_os=$env:ImageOS", "image_version=$env:ImageVersion", "python=$pyVersion",
        "ci_commit=$env:GITHUB_SHA", "run_id=$env:GITHUB_RUN_ID", "run_attempt=$env:GITHUB_RUN_ATTEMPT",
        "max_workers=$Workers"
    )
    if ($Result.firebase_sdk) {
        $metaArgs += "firebase_sdk_version=$($Result.firebase_sdk.version)", "firebase_sdk_sha256=$($Result.firebase_sdk.sha256)", "firebase_sdk_pinned=$($Result.firebase_sdk.pinned)"
    }
    $toolArgs = @('package', '--release-dir', $Release, '--native-manifest', (Join-Path $Meta 'NATIVE_BUNDLE_MANIFEST.json'),
        '--expect-engine-sha', $EngineSha, '--out-zip', $zip, '--out-manifest', $manifest)
    foreach ($m in $metaArgs) { $toolArgs += '--meta', $m }
    $Result.package = Invoke-Tool @toolArgs
    $Result.verify = Invoke-Tool verify-runtime --zip $zip --manifest $manifest `
        --expect-archive-sha $Result.package.archive_sha256 --expect-engine-sha $EngineSha `
        --native-manifest (Join-Path $Meta 'NATIVE_BUNDLE_MANIFEST.json')
    $Result.outputs = [ordered]@{
        archive = 'atermius-windows-x64.zip'; archive_sha256 = $Result.package.archive_sha256
        release_manifest_sha256 = Get-Sha256 $manifest
    }
    $Result.status = 'success'
    Exit-Group
} catch {
    $Result.error = "$($_.Exception.Message)"
    Write-Host "::error::Stage $($script:Stage) failed: $($_.Exception.Message)"
    throw
} finally {
    $Result.stage = $script:Stage
    $Result.stage_seconds[$script:Stage] = [math]::Round($script:StageClock.Elapsed.TotalSeconds, 1)
    $Result.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    if (Test-Path -LiteralPath $OutDir) {
        $Result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutDir 'build-result.json') -Encoding utf8NoBOM
    }
}
