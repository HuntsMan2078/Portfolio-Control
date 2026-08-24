# Portfolio Control v3.6.0 — Cross‑Platform Sync Edition

v3.6.0 在 v3.5.0 的 Windows + macOS、Local-first SQLite、Supabase 加密云同步基础上，新增 **宏观指标模块**：美国 2 年期国债收益率、10 年期国债收益率、2s10s 利差与 EFFR。宏观数据会进入总览、专门页面、提醒中心和 AI JSON。

## 宏观指标

- **US 2Y / US 10Y**：优先读取 U.S. Treasury 官方 Daily Treasury Par Yield Curve；若官网解析失败，自动回退 FRED 的 DGS2 / DGS10 日频数据。
- **2s10s**：自动计算 `10Y - 2Y`，单位 bp。
- **EFFR**：读取 Federal Reserve Bank of New York Markets Data API 的 Effective Federal Funds Rate。
- 显示 1 日 / 7 日 / 30 日变化、最近约 60 个交易日曲线和宏观提醒。
- 内置提醒包括：2Y 单日绝对变化 ≥ 10bp、约一周变化 ≥ 20bp、2s10s 倒挂。提醒只用于风险观察，不会自动触发买卖。
- 数据为官方/参考**日度数据，不是盘中实时行情**；模块缓存约 30 分钟，也可手动强制刷新。
- 不需要额外 API Key。

## 云同步设计

- Local-first：每台电脑仍然使用自己的 SQLite，断网不影响查看、记录、调仓和备忘。
- 自动同步：本机数据保存后，后台约 2 秒触发同步；启动时检查云端；断网后每 5 分钟重试，并在网络恢复时立即再试。
- 云端数据加密：`app_state` 先在本机用同步密码派生的 Fernet 密钥加密，再上传 Supabase。
- 云端不会保存：Longbridge OAuth Token、Marketaux Token、Supabase 登录密码、明文同步密码。
- 冲突保护：如果 Windows 和 Mac 都在上次同步后修改了数据，不会静默覆盖；界面会要求选择“采用云端”或“保留本机并上传”。
- 本地自动备份和 AI JSON 导出继续保留。

## 第一次配置 Supabase

1. 在 Supabase 创建一个免费 Project。
2. 在 `SQL Editor` 运行本包的 `supabase_setup.sql`。
3. 可在 `Authentication -> Users` 创建自己的邮箱用户；也可以在 Portfolio Control 的云同步设置里点“注册同步账户”。若项目要求邮箱验证，先完成验证后再登录。
4. 在 `Project Settings / API` 复制：
   - Project URL
   - Anon / Publishable Key
5. 打开 Portfolio Control -> `☁ 云同步设置`，填写 Project URL、Key、邮箱、Supabase 密码。
6. 另外设置一个 **Portfolio 同步密码（至少 8 位）**。两台电脑必须使用同一个同步密码，否则第二台无法解密第一台上传的数据。

> 同步密码不是 Supabase 登录密码。同步密码用于端到端式应用层加密；软件在本机只保存派生后的加密密钥。

## Windows 构建

在 Windows 10/11 解压本目录，双击：

`build_installer.bat`

成功后得到：

`release\PortfolioControlSetup_v3.6.0.exe`

目标电脑安装 Setup.exe 后不需要 Python。Longbridge CLI 会和 Windows 安装包一起打包，第一次运行只需 OAuth 授权。

## macOS 构建

在目标架构的 Mac 上打开 Terminal：

```bash
cd /path/to/portfolio_manager_v3_6_0
chmod +x build_macos.sh
./build_macos.sh
```

脚本会：

- 建立独立 Python build venv；
- 安装 PyInstaller、pywebview、cryptography；
- 使用长桥官方 macOS/Linux 安装脚本准备 Longbridge CLI，并把同架构二进制打进 `.app`；
- 生成 `Portfolio Control.app`；
- 使用 `hdiutil` 生成 `.dmg`。

输出：

`release-macos/PortfolioControl_v3.6.0_x86_64.dmg`（Intel Mac）

或：

`release-macos/PortfolioControl_v3.6.0_arm64.dmg`（Apple Silicon）

PyInstaller 不是跨平台/跨架构编译器：Intel DMG 建议在 Intel Mac 构建，Apple Silicon DMG 在 Apple Silicon Mac 构建。

### Gatekeeper / 正式签名

个人自用、未签名的 `.app/.dmg` 可能被 macOS Gatekeeper 提示来源不明。若有 Apple Developer ID，可在构建前设置：

```bash
export APPLE_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
./build_macos.sh
```


## 数据位置

Windows：

`%LOCALAPPDATA%\PortfolioControl\portfolio.db`

macOS：

`~/Library/Application Support/PortfolioControl/portfolio.db`

备份与 AI JSON 都在对应用户数据目录内。

## 两台电脑推荐流程

1. 先在当前主电脑升级到 v3.6.0。
2. 先手动“立即备份”一次。
3. 配置云同步并确认显示 `☁ 已同步`。
4. Mac 安装 v3.6.0。
5. Mac 配置相同 Supabase 账户 + **相同 Portfolio 同步密码**。
6. Mac 第一次连接时选择/执行“立即同步”，即可拉到相同组合。

如果两台电脑修改的是不同记录/字段，程序会尝试三方合并；如果两边修改了同一项且结果不同，会显示同步冲突，不会静默丢弃任何一边。
