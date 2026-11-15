> [!WARNING]
> **This repository is archived and no longer updated.** On 2026-11-14 it was merged into [aterm-project/atermius](https://github.com/aterm-project/atermius), where all development continues (this code now lives under `apps/client/`).
> Downloads for every platform are on the [Atermius releases page](https://github.com/aterm-project/atermius/releases).

<p align="center"><img src="docs/assets/atermius-mark.svg" width="72" alt="Atermius"></p>

<h1 align="center">Atermius · Desktop</h1>

<p align="center">An SSH workspace for Windows, macOS and Linux, with terminals, files and saved layouts.</p>
<p align="center"><a href="README.zh-CN.md">简体中文</a> · <a href="docs/INSTALLATION.en.md">Install</a> · <a href="docs/README.md">Documentation</a> · <a href="https://github.com/aterm-project/desktop/actions/workflows/checks.yml">CI</a></p>

## Start here

There is no published consumer installer matching the current main branch. Build from source using the guide. Desktop `ci-input-*` prereleases contain build inputs, not an installer. Do not treat a successful source CI run as a signed Windows/macOS/iOS/Android distribution.

- [Installation and build](docs/INSTALLATION.en.md)
- [First workspace and connection](docs/GETTING_STARTED.en.md)
- [Features and platform boundaries](docs/FEATURES.md)
- [Sharing Hosts](docs/SHARING.en.md)
- [Troubleshooting](docs/TROUBLESHOOTING.en.md)
- [Developer checks](docs/DEVELOPMENT.md)
- [All documentation](docs/README.md)

## What is included

- SSH terminals, compact Host lists, groups and reusable commands.
- SFTP/SCP file workspaces, split panes and saved window sets where supported.
- Local encrypted profiles or a self-hosted synchronized Vault.
- Organizations, member sharing and temporary links when enabled by the server.
- Chinese, Japanese, English and Russian UI; blue application accents and customizable terminal colors.

## Quick start

Flutter 3.47.5 / Dart 3.13; platform tools and the Windows RDP bundle are required.

```powershell
git clone https://github.com/aterm-project/desktop.git
cd desktop
flutter --version
flutter doctor -v
# Build the native RDP bundle first; follow docs/INSTALLATION.en.md.
# Then run flutter pub get/build from apps/client.
```

These repositories are currently private; your GitHub account needs access to clone them or download private artifacts. Detailed host prerequisites, output paths and update steps are in the [installation guide](docs/INSTALLATION.en.md).

## Support boundaries

Native Windows RDP requires the bundled engine; Android/iOS do not include that engine. Browser SSH requires an enabled, restricted Relay. Temporary browser SSH/RDP is a separate trusted-gateway flow. Native platform build/device acceptance is separate from the Linux Dart/widget checks in CI.

The full Flutter suite has disclosed historical failures. CI runs its documented focused checks; review the workflow and [validation documentation](docs/README.md) for their scope. Keep server state, account credentials and signing material outside Git.

## Repository layout

`apps/client/` contains the Flutter app; `packages/` contains five local dependency forks; `protocol/` contains shared contracts and test vectors. Only this repository’s platform runners are included.

Third-party licenses and upstream notices remain alongside their dependencies. Current source hashes are recorded in `SOURCE_SNAPSHOT.json`; initial import history is in `SOURCE_PROVENANCE.json`.

## Other repositories

[Server](https://github.com/aterm-project/server) · [Android](https://github.com/aterm-project/android) · [iOS](https://github.com/aterm-project/ios) · [Web](https://github.com/aterm-project/web)
