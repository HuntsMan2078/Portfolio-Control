Portfolio Control v3.7.1 — macOS Builder

1. 把整个 portfolio_manager_v3_6_0 文件夹放到 Mac。
2. 打开 Terminal，cd 进入目录。
3. 运行：

   chmod +x build_macos.sh
   ./build_macos.sh

4. 成功后在 release-macos/ 得到 DMG。

Intel Mac (x86_64) 和 Apple Silicon (arm64) 建议分别在对应架构机器构建。
长桥 CLI 使用官方安装地址并打进最终 App；第一次打开 Portfolio Control 时完成 OAuth 授权即可。

未签名版本可能触发 Gatekeeper。个人自用可以在系统设置的隐私与安全中允许打开；正式分发应使用 Apple Developer ID 签名及 notarization。
