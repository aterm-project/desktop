> [!WARNING]
> **本仓库已归档，不再更新。** 2026-11-14 已合并到 [aterm-project/atermius](https://github.com/aterm-project/atermius)，后续开发都在那里进行（本仓库的代码现位于 `apps/client/`）。
> 各平台安装包见 [Atermius 发布页](https://github.com/aterm-project/atermius/releases)。

<p align="center"><img src="docs/assets/atermius-mark.svg" width="72" alt="Atermius"></p>

<h1 align="center">Atermius · Desktop</h1>

<p align="center">面向 Windows、macOS 与 Linux 的 SSH 工作区，集成终端、文件管理和窗口布局。</p>
<p align="center"><a href="README.md">English</a> · <a href="docs/INSTALLATION.md">安装指南</a> · <a href="docs/README.md">文档导航</a> · <a href="https://github.com/aterm-project/desktop/actions/workflows/checks.yml">CI</a></p>

## 从这里开始

当前没有与最新主干对应的正式用户安装包，请按指南从源码构建。Desktop 的 `ci-input-*` 预发布资产是构建输入，不能当作安装包；源码 CI 通过也不代表已经提供各平台的签名发行包。

- [安装与构建](docs/INSTALLATION.md)
- [首次使用与连接主机](docs/GETTING_STARTED.md)
- [功能与平台边界](docs/FEATURES.md)
- [分享主机](docs/SHARING.md)
- [常见问题](docs/TROUBLESHOOTING.md)
- [开发与验证](docs/DEVELOPMENT.md)
- [全部文档](docs/README.md)

## 主要能力

- SSH 终端、紧凑主机列表、分组与快捷指令。
- SFTP/SCP 文件工作区、分屏和受平台支持的窗口组合。
- 加密本地资料，或连接自托管服务端使用同步 Vault。
- 服务端启用相应能力后，提供组织、成员分享与临时链接。
- 中、日、英、俄四种界面语言；蓝色应用强调色与可配置终端配色。

## 快速开始

Flutter 3.47.5 / Dart 3.13 与对应平台工具链；Windows 还需要先构建配套 RDP 引擎。

```powershell
git clone https://github.com/aterm-project/desktop.git
cd desktop
flutter --version
flutter doctor -v
# 先按 docs/INSTALLATION.md 构建原生 RDP bundle。
# 再在 apps/client 获取依赖并构建。
```

仓库目前为私有仓库，克隆和下载私有构建资产需要 GitHub 访问权限。完整的系统依赖、构建产物路径与更新步骤见[安装指南](docs/INSTALLATION.md)。

## 支持范围

Windows 原生 RDP 需要配套引擎，Android/iOS 不包含该原生引擎。浏览器常规 SSH 需要启用并限制目标范围的 Relay；临时浏览器 SSH/RDP 则是独立的受信网关流程。Linux 上的 Dart/组件 CI 不代表各原生平台已完成构建或设备验收。

完整 Flutter 套件仍有已披露的历史失败；仓库 CI 执行工作流中列出的针对性检查。具体范围见[验证与文档导航](docs/README.md)。服务端数据、账户凭据和签名材料不应提交到 Git。

## 源码结构

`apps/client/` 为 Flutter 应用，`packages/` 包含五个本地依赖分支，`protocol/` 保存协议与测试向量。本仓库只包含所属平台的启动工程。

第三方许可证与上游说明保留在相应依赖目录。`SOURCE_SNAPSHOT.json` 记录源码快照，`SOURCE_PROVENANCE.json` 保留首次导入来源。

## 其他仓库

[Server](https://github.com/aterm-project/server) · [Android](https://github.com/aterm-project/android) · [iOS](https://github.com/aterm-project/ios) · [Web](https://github.com/aterm-project/web)
