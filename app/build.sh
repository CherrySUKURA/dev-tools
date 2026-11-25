#!/bin/bash
# 构建 macOS 应用包：编译原生窗口外壳，组装 服务控制台.app，并做 ad-hoc 签名。
#
# 用法：
#   ./app/build.sh                 # 构建到 <控制台目录>/服务控制台.app
#   ./app/build.sh /path/xx.app    # 构建到指定位置（install.sh 用）
#
# 需要 Xcode Command Line Tools（提供 swiftc）。没有的话 install.sh 会退化成
# 浏览器入口，不影响网页方式使用。

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TARGET="${1:-$ROOT/服务控制台.app}"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "缺少 swiftc：请先安装 Xcode Command Line Tools（xcode-select --install）" >&2
  exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# 单文件编译，不依赖 Xcode 工程；-swift-version 5 保持宽松的并发检查。
swiftc -O -swift-version 5 \
  -framework Cocoa -framework WebKit \
  -o "$STAGE/TXLCConsole" "$HERE/shell.swift"

mkdir -p "$TARGET/Contents/MacOS" "$TARGET/Contents/Resources"
cp "$HERE/Info.plist" "$TARGET/Contents/Info.plist"
cp "$HERE/launcher.sh" "$TARGET/Contents/MacOS/launcher"
cp "$STAGE/TXLCConsole" "$TARGET/Contents/MacOS/TXLCConsole"
cp "$HERE/AppIcon.icns" "$TARGET/Contents/Resources/AppIcon.icns"
chmod +x "$TARGET/Contents/MacOS/launcher" "$TARGET/Contents/MacOS/TXLCConsole"

# ad-hoc 签名：本机自用足够；分发到别的机器时对方首次打开需右键“打开”。
codesign --force --deep --sign - "$TARGET" >/dev/null 2>&1 || {
  echo "警告：codesign 失败，应用仍可运行，但首次打开可能需要在“隐私与安全性”里放行。" >&2
}

echo "已生成：$TARGET"
