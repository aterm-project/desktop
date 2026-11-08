# Windows runtime CI (dedicated branch `ci/windows-runtime`)

This branch is orphaned. It holds only CI tooling, and product source never lives here. Each build consumes
frozen private inputs attached to a `ci-input-*` prerelease of this repository:

| Asset | Produced by (root, Linux) |
| --- | --- |
| `source.tar.gz`, `source-manifest.json` | `python3 -I winrt_tool.py make-source-bundle --tree <frozen desktop tree> --out-dir <stage>` |
| `native-bundle.zip` | `python3 -I winrt_tool.py make-native-bundle --from-zip <verified release ZIP> --out <stage>/native-bundle.zip` |

A run starts on a push that changes `tools/windows-ci/inputs.json`. That file holds exactly six keys: the tag
plus the SHA-256 values (see `inputs.json.example`). `workflow_dispatch` works only if the workflow file is also
present on the default branch, which is not planned.

## Pipeline (`build_windows_runtime.ps1`)

1. Validate the inputs. Check that the download holds exactly 3 assets and that the work and output dirs are empty.
2. `extract-native` and `extract-source` (strict, fail-closed):
   - verify the archive and manifest SHA-256
   - only regular files are allowed, with no links, devices or sparse members
   - reject traversal, absolute paths, drive letters, backslashes, reserved names, ADS `:` and trailing dot/space
   - reject exact and case-folded duplicates and file/dir collisions
   - require exact set equality with the manifest and an exact per-file SHA-256
   - write raw bytes, so EOL is never converted
   - reject `.git`, `.dart_tool`, build output, `flutter/ephemeral` and `Directory.Build.*` in frozen source
3. Check that the runner's Visual Studio has C++ x64 tools and ATL (`atlstr.h`, required by
   flutter_secure_storage_windows). This runs before the large downloads.
4. Install Flutter 3.47.5 (Windows ZIP). Verify its SHA-256 against the pin and the official `releases_windows.json`
   (sha256, framework revision and Dart 3.13.4), then verify `flutter --version --machine`.
5. Run `flutter pub get --enforce-lockfile` with a fresh `PUB_CACHE`. Then `check-lock` requires:
   - hosted packages come only from pub.dev and carry a sha256
   - path packages are relative and stay inside the archive
   - git packages are pinned to a 40-hex resolved-ref
   Then `reverify-source` runs.
6. Locate `firebase_core` through `.dart_tool/package_config.json`. Its `rootUri` may be relative to the config
   file or absolute. It is resolved with .NET `Uri(Uri base, string)` against the config file's absolute file URI,
   must be a `file:` URI inside the CI work root, and must contain `windows/CMakeLists.txt`.
   Download the Firebase C++ SDK for the version declared by the resolved `firebase_core` plugin. Hash it, check it
   against the optional pin and point `FIREBASE_CPP_SDK_DIR` at that verified copy. Otherwise the plugin's CMake
   would download it with no hash check.
7. Run `flutter build windows --release --no-pub` with:
   - `ATERMIUS_RDP_DIR` set to the extracted prebuilt engine (no native RDP rebuild)
   - at most 8 workers, set through `CMAKE_BUILD_PARALLEL_LEVEL`, `CL_MPCount`/`UseMultiToolTask`, and a
     `Directory.Build.rsp` with `-maxCpuCount:8` placed outside the frozen tree
   - no tests and no GUI
8. `reverify-source` again: any file the build regenerated (l10n, plugin registrant) fails the run. The report gives
   path, old/new hash and a `crlf_only` flag.
9. `package` copies only runtime files: top-level `.exe`/`.dll` and `data/**`. `.lib/.exp/.pdb/.ilk/...` are
   excluded and listed, and anything else fails the run. It checks that the engine, libusb and licenses match the
   native bundle, then writes a deterministic ZIP and `release-manifest.json`. `verify-runtime` then self-checks
   the package.
10. Upload artifact `atermius-windows-runtime-<run_id>-<attempt>`, which contains:
    - `atermius-windows-x64.zip` (+ `.sha256`)
    - `release-manifest.json`
    - `build-result.json`
    - `meta/`
    It is kept for 14 days.
