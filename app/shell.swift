// TXLC 服务控制台 —— 原生窗口外壳（macOS）。
//
// 作用：把 dashboard.py 提供的本地页面装进 WKWebView，双击 .app 得到的是
// 一个真正的应用窗口，而不是浏览器标签页。页面、配置和执行逻辑仍由
// dashboard.py / devctl.sh 负责，所以 App 入口和网页入口看到的是同一份数据，
// 也都能做配置（添加项目、端口、依赖、设置）。
//
// 依赖：仅系统自带的 Cocoa + WebKit，不需要 pip 安装任何东西。
// 编译：见同目录 build.sh（swiftc 单文件编译）。

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    // 默认地址对应 dashboard.py 的默认端口；可用命令行参数或环境变量覆盖。
    private var pageURL = URL(string: "http://localhost:18000/")!
    // 控制台目录（含 projects.conf 等配置文件），由 launcher 通过环境变量传入。
    private var consoleDir = ""

    private var window: NSWindow?
    private var webView: WKWebView?
    private var titleObserver: NSKeyValueObservation?
    // 服务刚启动时页面可能还连不上，失败后做有限次重试，避免一片空白。
    private var loadRetries = 0

    // ---------------------------------------------------------------
    // 启动
    // ---------------------------------------------------------------

    func applicationDidFinishLaunching(_ notification: Notification) {
        resolveStartupArguments()
        buildMenu()
        createWindow()
        loadPage()
        NSApp.activate(ignoringOtherApps: true)
    }

    /// 解析启动参数与环境变量：
    ///   1. 第一个非选项参数当作页面地址；
    ///   2. TXLC_DASHBOARD_URL 覆盖默认地址；
    ///   3. TXLC_CONSOLE_DIR 指明配置文件所在目录。
    private func resolveStartupArguments() {
        if let envURL = ProcessInfo.processInfo.environment["TXLC_DASHBOARD_URL"],
           let url = URL(string: envURL) {
            pageURL = url
        }
        for arg in CommandLine.arguments.dropFirst() where !arg.hasPrefix("-") {
            if let url = URL(string: arg), url.scheme != nil {
                pageURL = url
            }
            break
        }
        consoleDir = ProcessInfo.processInfo.environment["TXLC_CONSOLE_DIR"] ?? appDirectory()
    }

    /// 没拿到环境变量时的兜底：应用包在 <控制台目录>/服务控制台.app 下，向上两级即控制台目录。
    private func appDirectory() -> String {
        let bundleURL = Bundle.main.bundleURL
        if bundleURL.pathExtension == "app" {
            return bundleURL.deletingLastPathComponent().deletingLastPathComponent().path
        }
        return bundleURL.deletingLastPathComponent().path
    }

    private func createWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1360, height: 860)
        let window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "服务控制台"
        window.minSize = NSSize(width: 1000, height: 620)
        window.isReleasedWhenClosed = false
        // 页面是浅色的，强制浅色外观，避免系统深色模式下标题栏和内容割裂。
        window.appearance = NSAppearance(named: .aqua)
        window.setFrameAutosaveName("TXLCConsoleWindow")
        // 跟随当前桌面：双击启动时窗口出现在用户正在看的那个 Space，
        // 而不是被系统记到上次的桌面里（多显示器 + 多 Space 时尤其明显）。
        window.collectionBehavior.insert(.moveToActiveSpace)
        window.center()

        let configuration = WKWebViewConfiguration()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView

        // 页面标题变化时同步到窗口标题，日志里能看出当前在看哪个项目。
        titleObserver = webView.observe(\.title, options: [.new]) { [weak self] webView, _ in
            guard let self, let title = webView.title, !title.isEmpty else { return }
            self.window?.title = title
        }

        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        self.window = window
        self.webView = webView
    }

    private func loadPage() {
        webView?.load(URLRequest(url: pageURL))
    }

    // ---------------------------------------------------------------
    // 菜单
    // ---------------------------------------------------------------

    private func buildMenu() {
        let mainMenu = NSMenu()

        // 应用菜单：关于 / 隐藏 / 退出。
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "服务控制台")
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于服务控制台",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏服务控制台",
                        action: #selector(NSApplication.hide(_:)),
                        keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他",
                                         action: #selector(NSApplication.hideOtherApplications(_:)),
                                         keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "显示全部",
                        action: #selector(NSApplication.unhideAllApplications(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出服务控制台",
                        action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        mainMenu.addItem(appItem)

        // 控制台菜单：刷新 / 浏览器入口 / 配置文件 / 日志。
        let consoleItem = NSMenuItem()
        let consoleMenu = NSMenu(title: "控制台")
        consoleItem.submenu = consoleMenu
        consoleMenu.addItem(menuItem("重新加载页面", #selector(reloadPage), "r"))
        consoleMenu.addItem(menuItem("强制刷新", #selector(reloadFromOrigin), "r", [.command, .shift]))
        consoleMenu.addItem(.separator())
        consoleMenu.addItem(menuItem("在浏览器中打开（网页配置入口）",
                                     #selector(openInBrowser), "b", [.command, .shift]))
        consoleMenu.addItem(.separator())
        consoleMenu.addItem(menuItem("打开配置文件", #selector(openConfigFile), ""))
        consoleMenu.addItem(menuItem("打开配置目录", #selector(openConfigDirectory), ""))
        consoleMenu.addItem(menuItem("打开控制台日志", #selector(openConsoleLog), ""))
        mainMenu.addItem(consoleItem)

        // 编辑菜单：WKWebView 里的输入框（搜索、过滤、弹窗表单）依赖它才能复制粘贴。
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        mainMenu.addItem(editItem)

        // 窗口菜单：由系统接管最小化 / 缩放 / 前置。
        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "窗口")
        windowItem.submenu = windowMenu
        windowMenu.addItem(withTitle: "最小化",
                           action: #selector(NSWindow.performMiniaturize(_:)),
                           keyEquivalent: "m")
        windowMenu.addItem(withTitle: "缩放",
                           action: #selector(NSWindow.performZoom(_:)),
                           keyEquivalent: "")
        windowMenu.addItem(.separator())
        windowMenu.addItem(withTitle: "前置全部窗口",
                           action: #selector(NSApplication.arrangeInFront(_:)),
                           keyEquivalent: "")
        mainMenu.addItem(windowItem)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = mainMenu
    }

    private func menuItem(_ title: String, _ action: Selector, _ key: String,
                          _ modifiers: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        if !key.isEmpty { item.keyEquivalentModifierMask = modifiers }
        return item
    }

    // ---------------------------------------------------------------
    // 菜单动作
    // ---------------------------------------------------------------

    @objc private func reloadPage() {
        loadRetries = 0
        webView?.reload()
    }

    @objc private func reloadFromOrigin() {
        loadRetries = 0
        webView?.reloadFromOrigin()
    }

    /// 网页入口：同一份控制台用默认浏览器打开，方便多屏或调试。
    @objc private func openInBrowser() {
        NSWorkspace.shared.open(pageURL)
    }

    /// 直接定位到主配置文件；项目本身仍以 conf 文件为准，避免两套数据源。
    @objc private func openConfigFile() {
        let path = (consoleDir as NSString).appendingPathComponent("projects.conf")
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        } else {
            openConfigDirectory()
        }
    }

    @objc private func openConfigDirectory() {
        NSWorkspace.shared.open(URL(fileURLWithPath: consoleDir))
    }

    @objc private func openConsoleLog() {
        let path = "/tmp/txlc-dashboard.log"
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
    }

    // ---------------------------------------------------------------
    // WKWebView 回调
    // ---------------------------------------------------------------

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadRetries = 0
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    /// 服务没起来时（刚双击、或刚被关闭），每 0.4 秒重试一次，最多约 12 秒。
    private func retryLoad() {
        guard loadRetries < 30 else {
            showOfflineHint()
            return
        }
        loadRetries += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.webView?.load(URLRequest(url: self?.pageURL ?? URL(string: "http://localhost:18000/")!))
        }
    }

    /// 重试仍失败时给一页静态提示，比一片空白更容易判断是「服务关了」还是「网络挂了」。
    private func showOfflineHint() {
        let html = """
        <!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <style>
          body { margin: 0; height: 100vh; display: grid; place-items: center;
                 font-family: -apple-system, "PingFang SC", sans-serif; color: #0d1220;
                 background: #f5f6f8; }
          main { max-width: 520px; padding: 28px 30px; background: #fff;
                 border: 1px solid #e6e8ee; border-radius: 12px; }
          h1 { margin: 0 0 10px; font-size: 17px; }
          p { margin: 0 0 8px; color: #4b5563; font-size: 13px; line-height: 1.7; }
          code { padding: 1px 5px; font-size: 12px; background: #f1f3f6; border-radius: 4px; }
        </style></head><body><main>
          <h1>连不上控制台</h1>
          <p>本地服务没有响应，可能已被关闭，或端口被其他程序占用。</p>
          <p>关闭本窗口后重新双击「服务控制台」即可重新拉起服务；也可以按 ⌘R 重试。</p>
          <p>启动日志：<code>/tmp/txlc-dashboard.log</code></p>
        </main></body></html>
        """
        webView?.loadHTMLString(html, baseURL: nil)
    }

    /// 页面里的「日志 / 接口文档 / 页面入口」这类 target=_blank 链接交给系统浏览器，
    /// 不在 App 里再开一个没有地址栏的子窗口。
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    /// 站内链接（同一 host）留在窗口里，其他 host 一律用浏览器打开。
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url,
           url.host != nil, url.host != pageURL.host {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // ---------------------------------------------------------------
    // 应用生命周期
    // ---------------------------------------------------------------

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    /// 点 Dock 图标时把窗口重新叫出来。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window?.makeKeyAndOrderFront(nil) }
        return true
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
