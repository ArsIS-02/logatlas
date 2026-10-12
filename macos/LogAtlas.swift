// LogAtlas.app — окно LogAtlas для macOS (Apple Silicon и Intel).
//
// Окно показывает интерфейс LogAtlas через WKWebView (WebKit входит в macOS), без браузера и адресной строки.
// Сервер — встроенный Python: Contents/Resources/python/bin/python3 logatlas/launcher.pyw --window. Он сообщает
// порт строкой «LOGATLAS-READY <порт> app» в стандартный вывод, а завершается, когда окно закрывает его
// стандартный ввод (или само окно завершилось). Данные — ~/Library/Application Support/LogAtlas.
//
//   LogAtlas.app/Contents/MacOS/LogAtlas --selftest <файл>   проверка сборки: открыть интерфейс, записать
//                                                             версию из него в файл и выйти
// Переменные: LOGATLAS_NO_DIALOGS=1 — без окон сообщений (проверки), LOGATLAS_DEVTOOLS=1 — веб-инспектор.
// Сборка — macos/build.sh (swiftc из Xcode Command Line Tools).

import Cocoa
import WebKit

let appVersion = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "?"
let env = ProcessInfo.processInfo.environment
let noDialogs = env["LOGATLAS_NO_DIALOGS"] != nil

let dataDir: URL = {
    let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    let d = base.appendingPathComponent("LogAtlas", isDirectory: true)
    try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
    return d
}()

func wlog(_ text: String) {
    let f = dataDir.appendingPathComponent("window.log")
    let df = DateFormatter()
    df.dateFormat = "yyyy-MM-dd HH:mm:ss"
    let line = "\(df.string(from: Date())) \(text)\n"
    guard let data = line.data(using: .utf8) else { return }
    if let h = try? FileHandle(forWritingTo: f) {
        h.seekToEndOfFile()
        h.write(data)
        try? h.close()
    } else {
        try? data.write(to: f)
    }
}

let loadingHTML = """
<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>
:root { color-scheme: light dark; }
html, body { height: 100%; margin: 0; }
body { display: flex; align-items: center; justify-content: center; font: 15px -apple-system, sans-serif;
       color: #6b7785; background: #f6f7f9; }
@media (prefers-color-scheme: dark) { body { background: #15191e; color: #8b97a5; } }
</style></head><body><div>Запуск LogAtlas…</div></body></html>
"""

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var backend: Process?
    let stdinPipe = Pipe()
    var outBuffer = Data()
    var port = 0
    var baseURL: URL?
    var failed = false
    var quitting = false
    var downloads: [WKDownload: URL] = [:]
    var selftestFile: String?

    // ------------------------------------------------------------------------------------- запуск
    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--selftest"), i + 1 < args.count {
            selftestFile = args[i + 1]
            DispatchQueue.main.asyncAfter(deadline: .now() + 150) { [weak self] in
                self?.finishSelftest("timeout")
            }
        }
        wlog("запуск LogAtlas \(appVersion)")
        buildMenu()

        let cfg = WKWebViewConfiguration()
        // window.print() в WKWebView ничего не делает — печать отчёта выполняет окно
        let printJS = "window.print = function () { window.webkit.messageHandlers.logatlas.postMessage({cmd: 'print'}); };"
        cfg.userContentController.addUserScript(
            WKUserScript(source: printJS, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        cfg.userContentController.add(self, name: "logatlas")
        if env["LOGATLAS_DEVTOOLS"] != nil {
            cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        }

        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsMagnification = true
        web.allowsBackForwardNavigationGestures = false
        if #available(macOS 13.3, *) { web.isInspectable = env["LOGATLAS_DEVTOOLS"] != nil }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "LogAtlas"
        window.minSize = NSSize(width: 720, height: 480)
        window.contentView = web
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        if !window.setFrameUsingName("LogAtlasMain") { window.center() }
        window.setFrameAutosaveName("LogAtlasMain")
        window.makeKeyAndOrderFront(nil)
        web.loadHTMLString(loadingHTML, baseURL: nil)

        startBackend()
        NSApp.activate(ignoringOtherApps: true)
    }

    func startBackend() {
        guard let res = Bundle.main.resourceURL else { return }
        let py = res.appendingPathComponent("python/bin/python3")
        let script = res.appendingPathComponent("logatlas/launcher.pyw")
        guard FileManager.default.isExecutableFile(atPath: py.path),
              FileManager.default.fileExists(atPath: script.path) else {
            fail("В программе нет встроенного Python или launcher.pyw. Скачайте LogAtlas заново.")
            return
        }
        let p = Process()
        p.executableURL = py
        p.arguments = [script.path, "--window"]
        p.currentDirectoryURL = res.appendingPathComponent("logatlas")
        var e = env
        for k in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"] { e.removeValue(forKey: k) }
        e["PYTHONUTF8"] = "1"
        e["PYTHONNOUSERSITE"] = "1"
        e["PYTHONDONTWRITEBYTECODE"] = "1"      // программа подписана: в неё нельзя писать
        p.environment = e
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        p.standardInput = stdinPipe
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            if d.isEmpty {
                h.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async { self?.consume(d) }
        }
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            DispatchQueue.main.async { self?.backendExited(code) }
        }
        do {
            try p.run()
            backend = p
            wlog("сервер запущен: \(py.path) \(script.path) --window")
        } catch {
            fail("Не удалось запустить сервер LogAtlas: \(error.localizedDescription)")
        }
    }

    func consume(_ d: Data) {
        outBuffer.append(d)
        while let nl = outBuffer.firstIndex(of: 0x0A) {
            let lineData = outBuffer.subdata(in: outBuffer.startIndex..<nl)
            outBuffer.removeSubrange(outBuffer.startIndex...nl)
            let line = String(decoding: lineData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            if line.hasPrefix("LOGATLAS-READY ") {
                let parts = line.split(separator: " ")
                if parts.count >= 2, let p = Int(parts[1]) { ready(p) }
            } else if line.hasPrefix("LOGATLAS-ERROR ") {
                fail(String(line.dropFirst("LOGATLAS-ERROR ".count)))
            } else if !line.isEmpty {
                wlog("сервер: \(line)")
            }
        }
    }

    func ready(_ p: Int) {
        guard port == 0, let url = URL(string: "http://127.0.0.1:\(p)/") else { return }
        port = p
        baseURL = url
        wlog("сервер готов: \(url.absoluteString)")
        web.load(URLRequest(url: url))
    }

    func backendExited(_ code: Int32) {
        wlog("сервер завершился, код \(code)")
        if quitting || failed { return }
        failed = true
        let logPath = dataDir.appendingPathComponent("logatlas.log").path
        let text = port == 0
            ? "Сервер LogAtlas не запустился (код \(code)).\n\nПодробности — в журнале:\n\(logPath)"
            : "Сервер LogAtlas неожиданно остановился (код \(code)).\n\nПодробности — в журнале:\n\(logPath)"
        alert(text, style: .critical)
        finishSelftest("backend-exit \(code)")
        quitting = true
        NSApp.terminate(nil)
    }

    func fail(_ text: String) {
        if failed { return }
        failed = true
        wlog("ошибка: \(text)")
        alert(text, style: .critical)
        finishSelftest("error \(text)")
        quitting = true
        NSApp.terminate(nil)
    }

    func alert(_ text: String, style: NSAlert.Style = .informational) {
        wlog("сообщение: \(text)")
        if noDialogs { return }
        let a = NSAlert()
        a.messageText = "LogAtlas"
        a.informativeText = text
        a.alertStyle = style
        a.runModal()
    }

    // ------------------------------------------------------------------------------------- выход
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if quitting { return true }
        confirmQuit { ok in
            if ok {
                self.quitting = true
                NSApp.terminate(nil)
            }
        }
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateNow }
        confirmQuit { ok in
            if ok { self.quitting = true }
            NSApp.reply(toApplicationShouldTerminate: ok)
        }
        return .terminateLater
    }

    /// Спросить подтверждение, только если сервер сейчас разбирает или собирает журналы
    func confirmQuit(_ done: @escaping (Bool) -> Void) {
        guard let base = baseURL, let url = URL(string: "api/status", relativeTo: base) else { done(true); return }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var busy = false
            var name = ""
            if let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                busy = (obj["busy"] as? Bool) ?? false
                name = (obj["case"] as? String) ?? ""
            }
            DispatchQueue.main.async {
                if !busy || noDialogs { done(true); return }
                let a = NSAlert()
                a.messageText = name.isEmpty ? "Сейчас идёт разбор или сбор журналов." : "Сейчас идёт разбор «\(name)»."
                a.informativeText = "Если закрыть LogAtlas, он прервётся, и его нужно будет запустить заново."
                a.alertStyle = .warning
                a.addButton(withTitle: "Отмена")
                a.addButton(withTitle: "Закрыть LogAtlas")
                done(a.runModal() == .alertSecondButtonReturn)
            }
        }.resume()
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        try? stdinPipe.fileHandleForWriting.close()       // сервер видит конец ввода и завершается сам
        if let p = backend, p.isRunning {
            let deadline = Date().addingTimeInterval(4)
            while p.isRunning && Date() < deadline { usleep(50_000) }
            if p.isRunning { p.terminate() }
        }
        wlog("выход")
    }

    // ------------------------------------------------------------------------------------- навигация
    func isLocal(_ url: URL) -> Bool {
        url.host == "127.0.0.1" && port != 0 && url.port == port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        guard let url = navigationAction.request.url else { decisionHandler(.allow); return }
        let scheme = url.scheme ?? ""
        if scheme == "about" || scheme == "data" || scheme == "blob" || isLocal(url) {
            decisionHandler(.allow)
        } else {
            if ["http", "https", "mailto"].contains(scheme) { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let r = navigationResponse.response as? HTTPURLResponse,
           let cd = r.value(forHTTPHeaderField: "Content-Disposition"), cd.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
            return
        }
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let url = webView.url, isLocal(url) else { return }
        if let t = webView.title, !t.isEmpty { window.title = t }
        if selftestFile != nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                self?.web.evaluateJavaScript("(document.getElementById('ver') || {}).textContent || ''") { result, _ in
                    self?.finishSelftest("ok \(result as? String ?? "")")
                }
            }
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        wlog("процесс WebKit завершился — перезагружаю интерфейс")
        if let u = baseURL { webView.load(URLRequest(url: u)) }
    }

    // ------------------------------------------------------------------------------------- загрузки файлов
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let name = suggestedFilename.isEmpty ? "logatlas-download" : suggestedFilename
        let base = (name as NSString).deletingPathExtension
        let ext = (name as NSString).pathExtension
        var dest = dir.appendingPathComponent(name)
        var i = 1
        while FileManager.default.fileExists(atPath: dest.path) {
            dest = dir.appendingPathComponent(ext.isEmpty ? "\(base) (\(i))" : "\(base) (\(i)).\(ext)")
            i += 1
        }
        downloads[download] = dest
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let dest = downloads.removeValue(forKey: download) {
            wlog("сохранён файл: \(dest.path)")
            NSWorkspace.shared.activateFileViewerSelecting([dest])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloads.removeValue(forKey: download)
        alert("Не удалось сохранить файл: \(error.localizedDescription)", style: .warning)
    }

    // ------------------------------------------------------------------------------------- окна, файлы, диалоги страницы
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) { webView.load(navigationAction.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { r in
            completionHandler(r == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        alert(message)
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        if noDialogs { completionHandler(true); return }
        let a = NSAlert()
        a.messageText = "LogAtlas"
        a.informativeText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Отмена")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        if cmd == "print" { printPage() }
    }

    // ------------------------------------------------------------------------------------- меню
    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "О программе LogAtlas", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Скрыть LogAtlas", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Скрыть остальные",
                                         action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Показать все", action: #selector(NSApplication.unhideAllApplications(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Завершить LogAtlas", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "Файл")
        fileMenu.addItem(withTitle: "Главная", action: #selector(goHome), keyEquivalent: "1")
        fileMenu.addItem(withTitle: "Собрать журналы этого Mac…", action: #selector(collectHere), keyEquivalent: "")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "Открыть в браузере", action: #selector(openInBrowser), keyEquivalent: "")
        fileMenu.addItem(withTitle: "Папка данных", action: #selector(openDataDir), keyEquivalent: "")
        fileMenu.addItem(withTitle: "Журнал программы", action: #selector(openLog), keyEquivalent: "")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "Печать…", action: #selector(printPage), keyEquivalent: "p")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "Закрыть окно", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Правка")
        editMenu.addItem(withTitle: "Отменить", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Повторить", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Скопировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Выбрать все", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "Вид")
        viewMenu.addItem(withTitle: "Обновить", action: #selector(reloadPage), keyEquivalent: "r")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "Крупнее", action: #selector(zoomIn), keyEquivalent: "+")
        viewMenu.addItem(withTitle: "Мельче", action: #selector(zoomOut), keyEquivalent: "-")
        viewMenu.addItem(withTitle: "Обычный размер", action: #selector(zoomReset), keyEquivalent: "0")
        viewMenu.addItem(.separator())
        let fs = viewMenu.addItem(withTitle: "Во весь экран", action: #selector(NSWindow.toggleFullScreen(_:)),
                                  keyEquivalent: "f")
        fs.keyEquivalentModifierMask = [.command, .control]
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let winItem = NSMenuItem()
        let winMenu = NSMenu(title: "Окно")
        winMenu.addItem(withTitle: "Свернуть", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Изменить масштаб", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winItem.submenu = winMenu
        main.addItem(winItem)
        NSApp.windowsMenu = winMenu

        let helpItem = NSMenuItem()
        let helpMenu = NSMenu(title: "Справка")
        helpMenu.addItem(withTitle: "LogAtlas на GitHub", action: #selector(openGitHub), keyEquivalent: "")
        helpItem.submenu = helpMenu
        main.addItem(helpItem)
        NSApp.helpMenu = helpMenu

        NSApp.mainMenu = main
    }

    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "LogAtlas",
            .applicationVersion: appVersion,
            .version: "",
            .credits: NSAttributedString(string: "Анализатор журналов macOS, Windows и Linux\nhttps://github.com/ArsIS-02/logatlas"),
        ])
    }

    @objc func goHome() {
        if let u = baseURL { web.load(URLRequest(url: u)) }
    }

    @objc func collectHere() {
        web.evaluateJavaScript("window.laCommand && window.laCommand('collect-here')", completionHandler: nil)
    }

    @objc func openInBrowser() {
        if let u = baseURL { NSWorkspace.shared.open(u) }
    }

    @objc func openDataDir() { NSWorkspace.shared.open(dataDir) }

    @objc func openLog() {
        let f = dataDir.appendingPathComponent("logatlas.log")
        if FileManager.default.fileExists(atPath: f.path) {
            NSWorkspace.shared.activateFileViewerSelecting([f])
        } else {
            NSWorkspace.shared.open(dataDir)
        }
    }

    @objc func openGitHub() {
        if let u = URL(string: "https://github.com/ArsIS-02/logatlas") { NSWorkspace.shared.open(u) }
    }

    @objc func reloadPage() { web.reload() }
    @objc func zoomIn() { web.pageZoom = min(web.pageZoom + 0.1, 3.0) }
    @objc func zoomOut() { web.pageZoom = max(web.pageZoom - 0.1, 0.5) }
    @objc func zoomReset() { web.pageZoom = 1.0 }

    @objc func printPage() {
        let info = NSPrintInfo.shared
        info.horizontalPagination = .fit
        info.verticalPagination = .automatic
        info.isHorizontallyCentered = false
        info.isVerticallyCentered = false
        let op = web.printOperation(with: info)
        op.view?.frame = web.bounds
        op.runModal(for: window, delegate: nil, didRun: nil, contextInfo: nil)
    }

    // ------------------------------------------------------------------------------------- проверка сборки
    func finishSelftest(_ result: String) {
        guard let f = selftestFile else { return }
        selftestFile = nil
        wlog("проверка: \(result)")
        try? (result + "\n").write(toFile: f, atomically: true, encoding: .utf8)
        quitting = true
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
