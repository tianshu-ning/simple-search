// simple-search 原生窗口通过标准输入输出与常驻搜索进程通信。
import AppKit
import Carbon
import UniformTypeIdentifiers

let homeDir = FileManager.default.homeDirectoryForCurrentUser.path
let customDataDir = ProcessInfo.processInfo.environment["SIMPLE_SEARCH_HOME"] ?? ""
let dataDir = customDataDir.isEmpty ? homeDir + "/.simple-search" : customDataDir
let configURL = URL(fileURLWithPath: dataDir).appendingPathComponent("config.json")
let desktopAppIdentifier = "local.simple.search.desktop"
let appIdentifier = Bundle.main.bundleIdentifier ?? desktopAppIdentifier
let scriptPath = dataDir + "/app/scripts/simple_search_cli.py"
let panelWidth: CGFloat = 780
let headerHeight: CGFloat = 64
let tabsHeight: CGFloat = 48
let footerHeight: CGFloat = 40
let rowHeight: CGFloat = 58
let maxVisibleRows = 8

func findPython() -> String {
    let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", homeDir + "/.local/bin/python3.11", "/usr/bin/python3"]
    for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
        return c
    }
    return "/usr/bin/python3"
}

func readConfig() throws -> [String: Any] {
    let data: Data
    do {
        data = try Data(contentsOf: configURL)
    } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
        return [:]
    }
    guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw CocoaError(.fileReadCorruptFile)
    }
    return config
}

func writeConfig(_ config: [String: Any]) throws {
    try FileManager.default.createDirectory(at: configURL.deletingLastPathComponent(),
                                            withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
    try (data + Data([0x0A])).write(to: configURL, options: .atomic)
}

// MARK: - Model

struct ResultItem {
    let type: String
    let name: String
    let path: String
    let sub: String
    let r1: String
    let r2: String
    let ranges: [NSRange]
    let isDir: Bool
    let key: String
    let offline: Bool
    let bundle: String

    init(_ d: [String: Any]) {
        type = d["t"] as? String ?? "file"
        name = d["name"] as? String ?? ""
        path = d["path"] as? String ?? ""
        sub = d["sub"] as? String ?? ""
        r1 = d["r1"] as? String ?? ""
        r2 = d["r2"] as? String ?? ""
        isDir = d["dir"] as? Bool ?? false
        key = d["key"] as? String ?? ""
        offline = d["off"] as? Bool ?? false
        bundle = d["bundle"] as? String ?? ""
        var list: [NSRange] = []
        if let hl = d["hl"] as? [[Int]] {
            for pair in hl where pair.count == 2 {
                list.append(NSRange(location: pair[0], length: pair[1]))
            }
        }
        ranges = list
    }
}

// MARK: - Backend process

final class Backend {
    private var process: Process?
    private var input: FileHandle?
    private var buffer = Data()
    private var stopping = false
    var onMessage: (([String: Any]) -> Void)?

    func start() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: findPython())
        p.arguments = ["-I", scriptPath, "serve"]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        let inPipe = Pipe()
        let outPipe = Pipe()
        p.standardInput = inPipe
        p.standardOutput = outPipe
        let logPath = dataDir + "/server.log"
        FileManager.default.createFile(atPath: logPath, contents: nil, attributes: nil)
        if let log = FileHandle(forWritingAtPath: logPath) {
            p.standardError = log
        } else {
            p.standardError = FileHandle.nullDevice
        }
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async {
                self?.consume(data)
            }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                guard let self = self, !self.stopping else { return }
                self.onMessage?(["op": "restarting"])
                self.start()
            }
        }
        do {
            try p.run()
        } catch {
            onMessage?(["op": "status", "text": "无法启动搜索服务：\(error.localizedDescription)"])
            return
        }
        process = p
        input = inPipe.fileHandleForWriting
    }

    func stop() {
        stopping = true
        process?.terminate()
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer.subdata(in: buffer.startIndex..<newline)
            buffer.removeSubrange(buffer.startIndex...newline)
            if let obj = try? JSONSerialization.jsonObject(with: line, options: []) as? [String: Any] {
                onMessage?(obj)
            }
        }
    }

    func send(_ obj: [String: Any]) {
        guard let input = input, process?.isRunning == true,
              var data = try? JSONSerialization.data(withJSONObject: obj, options: []) else { return }
        data.append(10)
        try? input.write(contentsOf: data)
    }
}

// MARK: - Views

final class SearchPanel: NSPanel {
    override var canBecomeKey: Bool { return true }
    override var canBecomeMain: Bool { return true }
}

final class TabButton: NSButton {
    var isChosen = false {
        didSet { refresh() }
    }
    private let label: String

    init(label: String, index: Int, target: AnyObject, action: Selector) {
        self.label = label
        super.init(frame: .zero)
        self.tag = index
        self.target = target
        self.action = action
        isBordered = false
        refusesFirstResponder = true
        wantsLayer = true
        layer?.cornerRadius = 15
        refresh()
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    func refresh() {
        let color = isChosen ? NSColor.controlAccentColor : NSColor.secondaryLabelColor
        let font = NSFont.systemFont(ofSize: 14, weight: isChosen ? .semibold : .regular)
        attributedTitle = NSAttributedString(string: label, attributes: [.foregroundColor: color, .font: font])
        layer?.backgroundColor = isChosen ? NSColor.controlAccentColor.withAlphaComponent(0.13).cgColor : NSColor.clear.cgColor
    }

    override var intrinsicContentSize: NSSize {
        let s = super.intrinsicContentSize
        return NSSize(width: s.width + 24, height: 30)
    }
}

final class ResultRowView: NSTableRowView {
    override var isEmphasized: Bool {
        get { return false }
        set { }
    }

    override func drawSelection(in dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 10, dy: 2)
        NSColor.controlAccentColor.withAlphaComponent(0.14).setFill()
        NSBezierPath(roundedRect: r, xRadius: 10, yRadius: 10).fill()
    }
}

final class ResultCell: NSView {
    let iconView = NSImageView()
    let titleField = NSTextField(labelWithString: "")
    let subField = NSTextField(labelWithString: "")
    let right1 = NSTextField(labelWithString: "")
    let right2 = NSTextField(labelWithString: "")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        let views: [NSView] = [iconView, titleField, subField, right1, right2]
        for v in views {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        iconView.imageScaling = .scaleProportionallyUpOrDown
        titleField.font = NSFont.systemFont(ofSize: 16)
        titleField.lineBreakMode = .byTruncatingMiddle
        titleField.maximumNumberOfLines = 1
        subField.font = NSFont.systemFont(ofSize: 12)
        subField.textColor = NSColor.secondaryLabelColor
        subField.lineBreakMode = .byTruncatingMiddle
        subField.maximumNumberOfLines = 1
        for f in [right1, right2] {
            f.font = NSFont.systemFont(ofSize: 12)
            f.textColor = NSColor.secondaryLabelColor
            f.alignment = .right
            f.setContentCompressionResistancePriority(.required, for: .horizontal)
            f.setContentHuggingPriority(.required, for: .horizontal)
        }
        titleField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        subField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        NSLayoutConstraint.activate([
            iconView.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 24),
            iconView.centerYAnchor.constraint(equalTo: centerYAnchor),
            iconView.widthAnchor.constraint(equalToConstant: 32),
            iconView.heightAnchor.constraint(equalToConstant: 32),
            titleField.leadingAnchor.constraint(equalTo: iconView.trailingAnchor, constant: 14),
            titleField.topAnchor.constraint(equalTo: topAnchor, constant: 9),
            titleField.trailingAnchor.constraint(lessThanOrEqualTo: right1.leadingAnchor, constant: -12),
            subField.leadingAnchor.constraint(equalTo: titleField.leadingAnchor),
            subField.topAnchor.constraint(equalTo: titleField.bottomAnchor, constant: 2),
            subField.trailingAnchor.constraint(lessThanOrEqualTo: right2.leadingAnchor, constant: -12),
            right1.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -24),
            right1.firstBaselineAnchor.constraint(equalTo: titleField.firstBaselineAnchor),
            right2.trailingAnchor.constraint(equalTo: right1.trailingAnchor),
            right2.firstBaselineAnchor.constraint(equalTo: subField.firstBaselineAnchor),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    func configure(_ item: ResultItem, icon: NSImage) {
        iconView.image = icon
        let style = NSMutableParagraphStyle()
        style.lineBreakMode = .byTruncatingMiddle
        let text = NSMutableAttributedString(string: item.name, attributes: [
            .font: NSFont.systemFont(ofSize: 16),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: style,
        ])
        let length = (item.name as NSString).length
        for r in item.ranges where r.location >= 0 && r.length > 0 && r.location + r.length <= length {
            text.addAttribute(.foregroundColor, value: NSColor.controlAccentColor, range: r)
        }
        titleField.attributedStringValue = text
        subField.stringValue = item.sub
        right1.stringValue = item.r1
        right2.stringValue = item.r2
        alphaValue = item.offline ? 0.55 : 1.0
    }
}

// MARK: - Search window controller

final class SearchController: NSObject, NSTextFieldDelegate, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate {
    let backend = Backend()
    let panel: SearchPanel
    let field = NSTextField()
    let table = NSTableView()
    let scroll = NSScrollView()
    let footerLeft = NSTextField(labelWithString: "")
    let footerRight = NSTextField(labelWithString: "")
    let emptyLabel = NSTextField(labelWithString: "")
    var tabButtons: [TabButton] = []
    let tabKeys = ["all", "folder", "app", "doc", "image", "code", "topics", "bookmarks", "web_shortcuts"]
    let tabTitles = ["全部", "文件夹", "应用", "文档", "图片", "代码", "AI 对话", "书签", "网址"]
    var currentTab = 0
    var items: [ResultItem] = []
    var total = 0
    var elapsed = 0.0
    var recent = false
    var note = ""
    var ready = false
    var requestId = 0
    private var displayedRequestId = 0
    private var pendingReturn: Bool?
    var iconCache: [String: NSImage] = [:]
    var listHeight: NSLayoutConstraint!
    var toastTimer: Timer?
    var keyMonitor: Any?

    override init() {
        panel = SearchPanel(contentRect: NSRect(x: 0, y: 0, width: panelWidth, height: 400),
                            styleMask: [.borderless], backing: .buffered, defer: false)
        super.init()
        buildWindow()
        backend.onMessage = { [weak self] msg in
            self?.handle(msg)
        }
        backend.start()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.panel.isKeyWindow else { return event }
            return self.handleKey(event) ? nil : event
        }
    }

    // MARK: layout
    private func buildWindow() {
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.delegate = self

        let root = NSVisualEffectView()
        root.material = .popover
        root.blendingMode = .behindWindow
        root.state = .active
        root.wantsLayer = true
        root.layer?.cornerRadius = 16
        root.layer?.masksToBounds = true
        panel.contentView = root

        // Header: magnifier + search field
        let glass = NSImageView()
        if let img = NSImage(systemSymbolName: "magnifyingglass", accessibilityDescription: "搜索") {
            glass.image = img.withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 21, weight: .regular))
        }
        glass.contentTintColor = NSColor.secondaryLabelColor
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = NSFont.systemFont(ofSize: 26)
        field.placeholderString = "搜索文件、内容与 AI 对话"
        field.delegate = self
        field.cell?.usesSingleLineMode = true
        field.cell?.wraps = false
        field.cell?.isScrollable = true

        let sep1 = NSBox()
        sep1.boxType = .separator
        let sep2 = NSBox()
        sep2.boxType = .separator

        // Tabs
        let tabs = NSStackView()
        tabs.orientation = .horizontal
        tabs.spacing = 2
        for (i, t) in tabTitles.enumerated() {
            let b = TabButton(label: t, index: i, target: self, action: #selector(tabClicked(_:)))
            b.isChosen = i == 0
            tabButtons.append(b)
            tabs.addArrangedSubview(b)
        }

        // Results
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("main"))
        column.resizingMask = .autoresizingMask
        column.width = panelWidth
        table.addTableColumn(column)
        table.headerView = nil
        table.rowHeight = rowHeight
        table.intercellSpacing = NSSize(width: 0, height: 0)
        table.backgroundColor = .clear
        table.selectionHighlightStyle = .regular
        table.refusesFirstResponder = true
        table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        table.style = .plain
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(activateSelection)
        scroll.documentView = table
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        scroll.automaticallyAdjustsContentInsets = false
        scroll.contentInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)

        emptyLabel.font = NSFont.systemFont(ofSize: 14)
        emptyLabel.textColor = NSColor.tertiaryLabelColor
        emptyLabel.alignment = .center

        footerLeft.font = NSFont.systemFont(ofSize: 13)
        footerLeft.textColor = NSColor.secondaryLabelColor
        footerLeft.lineBreakMode = .byTruncatingTail
        footerLeft.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        footerRight.font = NSFont.systemFont(ofSize: 13)
        footerRight.textColor = NSColor.secondaryLabelColor
        footerRight.alignment = .right
        footerRight.setContentCompressionResistancePriority(.required, for: .horizontal)

        let all: [NSView] = [glass, field, sep1, tabs, scroll, emptyLabel, sep2, footerLeft, footerRight]
        for v in all {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }
        listHeight = scroll.heightAnchor.constraint(equalToConstant: 120)
        NSLayoutConstraint.activate([
            glass.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 26),
            glass.centerYAnchor.constraint(equalTo: root.topAnchor, constant: headerHeight / 2),
            glass.widthAnchor.constraint(equalToConstant: 24),
            field.leadingAnchor.constraint(equalTo: glass.trailingAnchor, constant: 14),
            field.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            field.centerYAnchor.constraint(equalTo: glass.centerYAnchor),
            sep1.topAnchor.constraint(equalTo: root.topAnchor, constant: headerHeight),
            sep1.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sep1.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            tabs.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 18),
            tabs.centerYAnchor.constraint(equalTo: sep1.bottomAnchor, constant: tabsHeight / 2),
            scroll.topAnchor.constraint(equalTo: sep1.bottomAnchor, constant: tabsHeight),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            listHeight,
            emptyLabel.centerXAnchor.constraint(equalTo: scroll.centerXAnchor),
            emptyLabel.centerYAnchor.constraint(equalTo: scroll.centerYAnchor),
            emptyLabel.widthAnchor.constraint(lessThanOrEqualTo: scroll.widthAnchor, constant: -40),
            sep2.topAnchor.constraint(equalTo: scroll.bottomAnchor),
            sep2.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sep2.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            footerLeft.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 26),
            footerLeft.centerYAnchor.constraint(equalTo: sep2.bottomAnchor, constant: footerHeight / 2),
            footerLeft.trailingAnchor.constraint(lessThanOrEqualTo: footerRight.leadingAnchor, constant: -16),
            footerRight.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            footerRight.centerYAnchor.constraint(equalTo: footerLeft.centerYAnchor),
        ])
        updateLayout()
        updateFooter()
    }

    private func updateLayout() {
        let rows = min(items.count, maxVisibleRows)
        let list: CGFloat = items.isEmpty ? 120 : CGFloat(rows) * rowHeight + 8
        listHeight.constant = list
        emptyLabel.isHidden = !items.isEmpty
        if !ready {
            emptyLabel.stringValue = "正在加载索引…"
        } else if showsNoteInResults {
            emptyLabel.stringValue = note
        } else if field.stringValue.isEmpty {
            emptyLabel.stringValue = "输入关键词开始搜索（支持拼音、ext:pdf、dm:week、content:发票）"
        } else {
            emptyLabel.stringValue = "没有找到「\(field.stringValue)」"
        }
        let height = headerHeight + 1 + tabsHeight + list + 1 + footerHeight
        var frame = panel.frame
        let top = frame.maxY
        frame.size.height = height
        frame.size.width = panelWidth
        frame.origin.y = top - height
        panel.setFrame(frame, display: true)
        panel.invalidateShadow()
    }

    private var showsNoteInResults: Bool {
        let tab = tabKeys[currentTab]
        return displayedRequestId == requestId && items.isEmpty && !note.isEmpty && (tab == "bookmarks" || tab == "web_shortcuts")
    }

    private func updateFooter() {
        if toastTimer != nil { return }
        if !ready {
            footerLeft.stringValue = "正在加载索引…"
        } else if recent && !showsNoteInResults {
            switch tabKeys[currentTab] {
            case "topics": footerLeft.stringValue = "最近的 AI 对话"
            case "bookmarks": footerLeft.stringValue = "最近添加的书签"
            default: footerLeft.stringValue = "最近修改的文件"
            }
        } else {
            let fmt = NumberFormatter()
            fmt.numberStyle = .decimal
            let n = fmt.string(from: NSNumber(value: total)) ?? String(total)
            var s = "共 \(n) 项 · \(String(format: "%.1f", elapsed)) ms"
            if !note.isEmpty && !showsNoteInResults { s += " · " + note }
            footerLeft.stringValue = s
        }
        if let item = selectedItem {
            switch item.type {
            case "topic":
                footerRight.stringValue = "↩ 打开会话   ⌘↩ 终端继续   ⌘C 拷贝命令"
            case "bookmark": footerRight.stringValue = "↩ 在原浏览器打开   ⌘↩ 默认浏览器   ⌘C 拷贝链接"
            case "web_shortcut":
                footerRight.stringValue = item.path.isEmpty ? "请在关键词后输入搜索词" : "↩ 默认浏览器打开   ⌘C 拷贝链接"
            default: footerRight.stringValue = "↩ 打开   ⌘↩ 访达中显示   ⌘C 拷贝路径   ⌘Y 预览"
            }
        } else {
            footerRight.stringValue = "← → 切换分类   esc 关闭"
        }
    }

    private func toast(_ text: String) {
        toastTimer?.invalidate()
        footerLeft.stringValue = text
        toastTimer = Timer.scheduledTimer(withTimeInterval: 1.6, repeats: false) { [weak self] _ in
            self?.toastTimer = nil
            self?.updateFooter()
        }
    }

    // MARK: show / hide
    func toggle() {
        if panel.isVisible && NSApp.isActive {
            hide()
        } else {
            show()
        }
    }

    func show() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.main
        if let vis = screen?.visibleFrame {
            let h = panel.frame.height
            let top = vis.maxY - vis.height * 0.18
            panel.setFrame(NSRect(x: vis.midX - panelWidth / 2, y: top - h, width: panelWidth, height: h), display: false)
        }
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(field)
        field.currentEditor()?.selectAll(nil)
        if ready { runSearch() }
    }

    func hide() {
        pendingReturn = nil
        panel.orderOut(nil)
        NSApp.hide(nil)
    }

    func windowDidResignKey(_ notification: Notification) {
        if panel.isVisible { hide() }
    }

    // MARK: search
    func runSearch() {
        pendingReturn = nil
        requestId += 1
        backend.send(["op": "search", "id": requestId, "q": field.stringValue, "tab": tabKeys[currentTab], "limit": 80])
    }

    func controlTextDidChange(_ obj: Notification) {
        if let editor = field.currentEditor() as? NSTextView, editor.hasMarkedText() { return }
        runSearch()
    }

    private func handle(_ msg: [String: Any]) {
        let op = msg["op"] as? String ?? ""
        switch op {
        case "ready":
            ready = true
            runSearch()
            updateLayout()
            updateFooter()
        case "restarting":
            ready = false
            updateFooter()
        case "status":
            if !ready { footerLeft.stringValue = msg["text"] as? String ?? "" }
        case "results":
            let id = msg["id"] as? Int ?? 0
            if id < requestId { return }
            displayedRequestId = id
            let raw = msg["items"] as? [[String: Any]] ?? []
            items = raw.map { ResultItem($0) }
            total = msg["total"] as? Int ?? items.count
            elapsed = msg["ms"] as? Double ?? 0
            recent = msg["recent"] as? Bool ?? false
            note = msg["note"] as? String ?? ""
            table.reloadData()
            if !items.isEmpty {
                table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
                table.scrollRowToVisible(0)
            }
            updateLayout()
            updateFooter()
            if let secondary = pendingReturn {
                pendingReturn = nil
                if secondary { secondaryAction() } else { activateSelection() }
            }
        case "toast":
            toast(msg["text"] as? String ?? "")
        case "copy":
            let text = msg["text"] as? String ?? ""
            if !text.isEmpty {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            }
            toast(msg["toast"] as? String ?? "已拷贝")
        default:
            break
        }
    }

    // MARK: table
    func numberOfRows(in tableView: NSTableView) -> Int {
        return items.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let id = NSUserInterfaceItemIdentifier("result")
        var cell = tableView.makeView(withIdentifier: id, owner: nil) as? ResultCell
        if cell == nil {
            cell = ResultCell(frame: .zero)
            cell?.identifier = id
        }
        let item = items[row]
        cell?.configure(item, icon: icon(for: item))
        return cell
    }

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        return ResultRowView()
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        updateFooter()
    }

    private var selectedItem: ResultItem? {
        let r = table.selectedRow
        return r >= 0 && r < items.count ? items[r] : nil
    }

    private func icon(for item: ResultItem) -> NSImage {
        if item.type == "web_shortcut" {
            return NSImage(systemSymbolName: "globe", accessibilityDescription: nil) ?? NSImage()
        }
        if item.type == "bookmark" {
            let key = "#bm:" + item.bundle
            if let cached = iconCache[key] { return cached }
            var img = NSImage(systemSymbolName: "bookmark", accessibilityDescription: nil) ?? NSImage()
            if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: item.bundle) {
                img = NSWorkspace.shared.icon(forFile: app.path)
            }
            iconCache[key] = img
            return img
        }
        if item.type == "topic" {
            if let cached = iconCache["#topic"] { return cached }
            let img = NSImage(systemSymbolName: "bubble.left.and.bubble.right", accessibilityDescription: nil)?
                .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 22, weight: .regular)) ?? NSImage()
            iconCache["#topic"] = img
            return img
        }
        let ext = (item.path as NSString).pathExtension.lowercased()
        let key: String
        if item.isDir {
            key = "#dir"
        } else if ext == "app" || ext.isEmpty {
            key = item.path
        } else {
            key = ext
        }
        if let cached = iconCache[key] { return cached }
        let img: NSImage
        if item.isDir {
            img = NSWorkspace.shared.icon(for: .folder)
        } else if ext == "app" || ext.isEmpty {
            img = NSWorkspace.shared.icon(forFile: item.path)
        } else if let t = UTType(filenameExtension: ext) {
            img = NSWorkspace.shared.icon(for: t)
        } else {
            img = NSWorkspace.shared.icon(for: .data)
        }
        if iconCache.count > 500 { iconCache.removeAll() }
        iconCache[key] = img
        return img
    }

    // MARK: keyboard
    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        switch commandSelector {
        case #selector(NSResponder.moveDown(_:)):
            move(1)
            return true
        case #selector(NSResponder.moveUp(_:)):
            move(-1)
            return true
        case #selector(NSResponder.scrollPageDown(_:)), #selector(NSResponder.pageDown(_:)):
            move(maxVisibleRows)
            return true
        case #selector(NSResponder.scrollPageUp(_:)), #selector(NSResponder.pageUp(_:)):
            move(-maxVisibleRows)
            return true
        case #selector(NSResponder.moveLeft(_:)):
            // ← switches to the previous category when the caret has nowhere further to go.
            let sel = textView.selectedRange()
            if sel.length == 0 && sel.location == 0 && currentTab > 0 {
                selectTab(currentTab - 1)
                return true
            }
            return false
        case #selector(NSResponder.moveRight(_:)):
            let sel = textView.selectedRange()
            let length = (textView.string as NSString).length
            if sel.length == 0 && sel.location >= length && currentTab < tabKeys.count - 1 {
                selectTab(currentTab + 1)
                return true
            }
            return false
        case #selector(NSResponder.insertNewline(_:)):
            handleReturn(secondary: false)
            return true
        case #selector(NSResponder.cancelOperation(_:)):
            hide()
            return true
        case #selector(NSResponder.insertTab(_:)):
            selectTab((currentTab + 1) % tabKeys.count)
            return true
        case #selector(NSResponder.insertBacktab(_:)):
            selectTab((currentTab + tabKeys.count - 1) % tabKeys.count)
            return true
        default:
            return false
        }
    }

    private func handleKey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard flags.contains(.command) else { return false }
        let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch event.keyCode {
        case 36, 76:                       // return / enter
            handleReturn(secondary: true)
            return true
        case 8:                            // C
            if flags.contains(.shift) { return false }
            if let editor = field.currentEditor(), editor.selectedRange.length > 0, !flags.contains(.option) {
                return false
            }
            copySelection(nameOnly: flags.contains(.option))
            return true
        case 16:                           // Y
            preview()
            return true
        case 13:                           // W
            hide()
            return true
        case 15:                           // R
            backend.send(["op": "refresh"])
            return true
        default:
            break
        }
        if let d = Int(chars), d >= 1, d <= tabKeys.count {
            selectTab(d - 1)
            return true
        }
        return false
    }

    private func move(_ delta: Int) {
        guard !items.isEmpty else { return }
        let next = max(0, min(items.count - 1, table.selectedRow + delta))
        table.selectRowIndexes(IndexSet(integer: next), byExtendingSelection: false)
        table.scrollRowToVisible(next)
    }

    @objc func tabClicked(_ sender: NSButton) {
        selectTab(sender.tag)
        panel.makeFirstResponder(field)
    }

    private func selectTab(_ index: Int) {
        currentTab = index
        for (i, b) in tabButtons.enumerated() {
            b.isChosen = i == index
        }
        runSearch()
    }

    // MARK: actions
    private func handleReturn(secondary: Bool) {
        let tab = tabKeys[currentTab]
        if ready && displayedRequestId != requestId && (tab == "web_shortcuts" || (tab == "all" && !field.stringValue.isEmpty)) {
            // 等待当前分类和输入对应的结果，避免快速回车打开上一条网址。
            pendingReturn = secondary
            return
        }
        if secondary { secondaryAction() } else { activateSelection() }
    }

    @objc func activateSelection() {
        guard let item = selectedItem else { return }
        if item.type == "web_shortcut" {
            openWebShortcut(item)
            return
        }
        if item.type == "topic" {
            backend.send(["op": "topic_open", "key": item.key])
            hide()
            return
        }
        if item.type == "bookmark" {
            openURL(item.path, bundle: item.bundle)
            hide()
            return
        }
        if item.offline {
            toast("所在磁盘未连接")
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: item.path))
        hide()
    }

    private func secondaryAction() {
        guard let item = selectedItem else { return }
        if item.type == "web_shortcut" {
            openWebShortcut(item)
            return
        }
        if item.type == "topic" {
            backend.send(["op": "topic_terminal", "key": item.key])
            hide()
            return
        }
        if item.type == "bookmark" {
            openURL(item.path, bundle: "")
            hide()
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: item.path)])
        hide()
    }

    private func copySelection(nameOnly: Bool) {
        guard let item = selectedItem else { return }
        if item.type == "topic" {
            if nameOnly {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(item.name, forType: .string)
                toast("已拷贝标题")
            } else {
                backend.send(["op": "topic_copy", "key": item.key])
            }
            return
        }
        let text = nameOnly ? item.name : item.path
        if item.type == "bookmark" || item.type == "web_shortcut" {
            if item.type == "web_shortcut" {
                guard displayedRequestId == requestId else { return }
                if !nameOnly && item.path.isEmpty {
                    toast("请输入搜索词")
                    return
                }
            }
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            toast(nameOnly ? "已拷贝标题" : "已拷贝链接：" + text)
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        toast("已拷贝：" + text)
    }

    private func preview() {
        guard let item = selectedItem, item.type == "file", !item.offline else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/qlmanage")
        p.arguments = ["-p", item.path]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }

    func refreshIndexes() {
        backend.send(["op": "refresh"])
    }

    func reloadWebShortcuts() {
        backend.send(["op": "reload_web_shortcuts"])
        if ready { runSearch() }
    }

    private func openWebShortcut(_ item: ResultItem) {
        guard displayedRequestId == requestId else { return }
        guard !item.path.isEmpty else {
            toast("请输入搜索词")
            return
        }
        if openURL(item.path, bundle: "") {
            hide()
        } else {
            toast("无法用默认浏览器打开网址")
        }
    }

    @discardableResult
    private func openURL(_ text: String, bundle: String) -> Bool {
        guard let url = URL(string: text) else { return false }
        if !bundle.isEmpty, let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundle) {
            NSWorkspace.shared.open([url], withApplicationAt: app, configuration: NSWorkspace.OpenConfiguration(), completionHandler: nil)
            return true
        }
        return NSWorkspace.shared.open(url)
    }

}

// MARK: - 全局快捷键

private enum ShortcutModifier: String, CaseIterable {
    case control, option, command, shift

    init?(token: String) {
        switch token {
        case "ctrl": self = .control
        case "alt", "opt": self = .option
        case "cmd": self = .command
        default: self.init(rawValue: token)
        }
    }

    var title: String { rawValue.capitalized }

    var carbonFlag: UInt32 {
        switch self {
        case .control: return UInt32(controlKey)
        case .option: return UInt32(optionKey)
        case .command: return UInt32(cmdKey)
        case .shift: return UInt32(shiftKey)
        }
    }

    var eventFlag: CGEventFlags {
        switch self {
        case .control: return .maskControl
        case .option: return .maskAlternate
        case .command: return .maskCommand
        case .shift: return .maskShift
        }
    }

    func matches(keyCode: Int64) -> Bool {
        switch self {
        case .control: return keyCode == kVK_Control || keyCode == kVK_RightControl
        case .option: return keyCode == kVK_Option || keyCode == kVK_RightOption
        case .command: return keyCode == kVK_Command || keyCode == kVK_RightCommand
        case .shift: return keyCode == kVK_Shift || keyCode == kVK_RightShift
        }
    }
}

private enum Shortcut: Equatable {
    case combination(key: String, modifiers: Set<ShortcutModifier>)
    case doubleTap(ShortcutModifier)

    static let defaultValue = "alt+space"
    static let keyCodes: [String: Int] = [
        "a": kVK_ANSI_A, "b": kVK_ANSI_B, "c": kVK_ANSI_C, "d": kVK_ANSI_D, "e": kVK_ANSI_E,
        "f": kVK_ANSI_F, "g": kVK_ANSI_G, "h": kVK_ANSI_H, "i": kVK_ANSI_I, "j": kVK_ANSI_J,
        "k": kVK_ANSI_K, "l": kVK_ANSI_L, "m": kVK_ANSI_M, "n": kVK_ANSI_N, "o": kVK_ANSI_O,
        "p": kVK_ANSI_P, "q": kVK_ANSI_Q, "r": kVK_ANSI_R, "s": kVK_ANSI_S, "t": kVK_ANSI_T,
        "u": kVK_ANSI_U, "v": kVK_ANSI_V, "w": kVK_ANSI_W, "x": kVK_ANSI_X, "y": kVK_ANSI_Y, "z": kVK_ANSI_Z,
        "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3, "4": kVK_ANSI_4,
        "5": kVK_ANSI_5, "6": kVK_ANSI_6, "7": kVK_ANSI_7, "8": kVK_ANSI_8, "9": kVK_ANSI_9,
        "-": kVK_ANSI_Minus, "=": kVK_ANSI_Equal, "[": kVK_ANSI_LeftBracket, "]": kVK_ANSI_RightBracket,
        "\\": kVK_ANSI_Backslash, ";": kVK_ANSI_Semicolon, "'": kVK_ANSI_Quote, "`": kVK_ANSI_Grave,
        ",": kVK_ANSI_Comma, ".": kVK_ANSI_Period, "/": kVK_ANSI_Slash,
        "space": kVK_Space, "tab": kVK_Tab, "return": kVK_Return, "escape": kVK_Escape,
        "delete": kVK_Delete, "forwarddelete": kVK_ForwardDelete,
        "up": kVK_UpArrow, "down": kVK_DownArrow, "left": kVK_LeftArrow, "right": kVK_RightArrow,
        "home": kVK_Home, "end": kVK_End, "pageup": kVK_PageUp, "pagedown": kVK_PageDown,
        "f1": kVK_F1, "f2": kVK_F2, "f3": kVK_F3, "f4": kVK_F4, "f5": kVK_F5, "f6": kVK_F6,
        "f7": kVK_F7, "f8": kVK_F8, "f9": kVK_F9, "f10": kVK_F10, "f11": kVK_F11, "f12": kVK_F12,
        "f13": kVK_F13, "f14": kVK_F14, "f15": kVK_F15, "f16": kVK_F16, "f17": kVK_F17,
        "f18": kVK_F18, "f19": kVK_F19, "f20": kVK_F20,
        "keypad0": kVK_ANSI_Keypad0, "keypad1": kVK_ANSI_Keypad1, "keypad2": kVK_ANSI_Keypad2,
        "keypad3": kVK_ANSI_Keypad3, "keypad4": kVK_ANSI_Keypad4, "keypad5": kVK_ANSI_Keypad5,
        "keypad6": kVK_ANSI_Keypad6, "keypad7": kVK_ANSI_Keypad7, "keypad8": kVK_ANSI_Keypad8,
        "keypad9": kVK_ANSI_Keypad9, "keypadenter": kVK_ANSI_KeypadEnter, "keypadplus": kVK_ANSI_KeypadPlus,
        "keypadminus": kVK_ANSI_KeypadMinus, "keypadmultiply": kVK_ANSI_KeypadMultiply,
        "keypaddivide": kVK_ANSI_KeypadDivide, "keypaddecimal": kVK_ANSI_KeypadDecimal,
        "keypadequals": kVK_ANSI_KeypadEquals, "keypadclear": kVK_ANSI_KeypadClear,
    ]

    static func keyTitle(_ key: String) -> String {
        let titles = [
            "space": "空格", "tab": "Tab", "return": "回车", "escape": "Esc", "delete": "删除",
            "forwarddelete": "向前删除", "up": "上方向键", "down": "下方向键", "left": "左方向键",
            "right": "右方向键", "home": "Home", "end": "End", "pageup": "Page Up", "pagedown": "Page Down",
            "keypadenter": "数字键盘回车", "keypadplus": "数字键盘加号", "keypadminus": "数字键盘减号",
            "keypadmultiply": "数字键盘乘号", "keypaddivide": "数字键盘除号", "keypaddecimal": "数字键盘小数点",
            "keypadequals": "数字键盘等号", "keypadclear": "数字键盘清除",
        ]
        if let title = titles[key] { return title }
        if key.hasPrefix("keypad") { return "数字键盘" + key.dropFirst(6) }
        return key.uppercased()
    }

    init?(_ text: String) {
        let parts = text.lowercased().components(separatedBy: "+").map {
            $0.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if parts.count == 2, parts[0] == "double", let modifier = ShortcutModifier(token: parts[1]) {
            self = .doubleTap(modifier)
            return
        }
        var modifiers = Set<ShortcutModifier>()
        var key: String?
        for part in parts {
            if let modifier = ShortcutModifier(token: part), modifiers.insert(modifier).inserted {
                continue
            }
            // 旧配置中的 Hyper 表示同时按住这四个修饰键。
            if part == "hyper", modifiers.isEmpty {
                modifiers = Set(ShortcutModifier.allCases)
                continue
            }
            guard key == nil, Self.keyCodes[part] != nil else { return nil }
            key = part
        }
        guard let key = key, !modifiers.isEmpty else { return nil }
        self = .combination(key: key, modifiers: modifiers)
    }

    var configValue: String {
        switch self {
        case .combination(let key, let modifiers):
            return (ShortcutModifier.allCases.filter { modifiers.contains($0) }.map(\.rawValue) + [key]).joined(separator: "+")
        case .doubleTap(let modifier):
            return "double+" + modifier.rawValue
        }
    }

    var title: String {
        switch self {
        case .combination(let key, let modifiers):
            return (ShortcutModifier.allCases.filter { modifiers.contains($0) }.map(\.title) + [Self.keyTitle(key)]).joined(separator: " + ")
        case .doubleTap(let modifier):
            return "双击 " + modifier.title
        }
    }

    var conflictsWithSearchWindow: Bool {
        guard case .combination(let key, let modifiers) = self else { return false }
        if modifiers.contains(.command) {
            // 搜索窗口的选择、预览、关闭和刷新操作接受附加修饰键。
            if ["y", "w", "r", "return", "keypadenter"].contains(key) { return true }
            if !modifiers.contains(.shift) && ["c", "1", "2", "3", "4", "5", "6", "7", "8"].contains(key) { return true }
            return modifiers == [.command] && ["z", "x", "v", "a"].contains(key)
        }
        return modifiers == [.shift] && key == "tab"
    }

    func checkAvailability() throws {
        switch self {
        case .doubleTap:
            guard CGPreflightListenEventAccess() else { throw ShortcutError.inputMonitoring }
        case .combination(let key, let modifiers):
            guard let code = Self.keyCodes[key], !modifiers.isEmpty else { throw ShortcutError.invalid }
            guard !conflictsWithSearchWindow else { throw ShortcutError.searchWindowConflict }
            let flags = modifiers.reduce(UInt32(0)) { $0 | $1.carbonFlag }
            guard !Self.conflictsWithSystem(keyCode: UInt32(code), flags: flags) else { throw ShortcutError.systemConflict }
        }
    }

    private static func conflictsWithSystem(keyCode: UInt32, flags: UInt32) -> Bool {
        var keys: Unmanaged<CFArray>?
        guard CopySymbolicHotKeys(&keys) == noErr,
              let entries = keys?.takeRetainedValue() as? [[String: NSNumber]] else { return false }
        let shortcutFlags = UInt32(cmdKey | controlKey | optionKey | shiftKey | kEventKeyModifierFnMask)
        return entries.contains { entry in
            entry["kHISymbolicHotKeyEnabled"]?.boolValue == true &&
                entry["kHISymbolicHotKeyCode"]?.uint32Value == keyCode &&
                entry["kHISymbolicHotKeyModifiers"].map { $0.uint32Value & shortcutFlags } == flags
        }
    }
}

private enum ShortcutError: LocalizedError, Equatable {
    case invalid
    case configRead
    case configWrite
    case searchWindowConflict
    case systemConflict
    case registeredConflict
    case inputMonitoring
    case eventMonitorUnavailable
    case registrationFailed(OSStatus)

    var errorDescription: String? {
        switch self {
        case .invalid: return "快捷键格式无效。"
        case .configRead: return "无法读取配置文件，请检查文件权限和 JSON 格式。"
        case .configWrite: return "无法保存快捷键，请检查配置目录的写入权限。"
        case .searchWindowConflict: return "此组合键已用于搜索窗口中的其他操作，请换一个。"
        case .systemConflict: return "此组合键已被 macOS 系统快捷键占用，请换一个。"
        case .registeredConflict: return "此组合键已被系统或其他应用注册，请换一个。"
        case .inputMonitoring: return "请在「系统设置 → 隐私与安全性 → 输入监控」中允许 simple-search。如系统提示，请重新打开应用。"
        case .eventMonitorUnavailable: return "无法启用双击监听。如果刚授予输入监控权限，请重新打开 simple-search 后重试。"
        case .registrationFailed(let status): return "macOS 无法注册此组合键（错误代码 \(status)），请换一个。"
        }
    }

    var needsInputMonitoring: Bool {
        if case .inputMonitoring = self { return true }
        return false
    }
}

private struct ModifierDoubleTap {
    private static let interval: UInt64 = 400_000_000
    let modifier: ShortcutModifier
    private var press: (keyCode: Int64, time: UInt64)?
    private var lastRelease: UInt64?

    init(_ modifier: ShortcutModifier) {
        self.modifier = modifier
    }

    mutating func reset() {
        press = nil
        lastRelease = nil
    }

    mutating func handle(type: CGEventType, keyCode: Int64, flags: CGEventFlags, time: UInt64) -> Bool {
        let heldModifiers = flags.intersection([.maskControl, .maskAlternate, .maskCommand, .maskShift, .maskSecondaryFn])
        guard type == .flagsChanged, modifier.matches(keyCode: keyCode) else {
            reset()
            return false
        }
        if heldModifiers == modifier.eventFlag, press == nil {
            press = (keyCode, time)
            return false
        }
        guard heldModifiers.isEmpty, let press = press, press.keyCode == keyCode,
              time >= press.time, time - press.time <= Self.interval else {
            reset()
            return false
        }
        let isDoubleTap = lastRelease.map { press.time >= $0 && time - $0 <= Self.interval } ?? false
        self.press = nil
        lastRelease = isDoubleTap ? nil : time
        return isDoubleTap
    }
}

private final class ModifierTapMonitor {
    private var gesture: ModifierDoubleTap
    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private let action: () -> Void

    init(modifier: ShortcutModifier, action: @escaping () -> Void) throws {
        gesture = ModifierDoubleTap(modifier)
        self.action = action
        // 普通键和鼠标事件仅用于取消本次双击判定。
        let events: [CGEventType] = [.flagsChanged, .keyDown, .leftMouseDown, .rightMouseDown, .otherMouseDown, .scrollWheel]
        let mask = events.reduce(CGEventMask(0)) { $0 | (1 << $1.rawValue) }
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                                          options: .listenOnly, eventsOfInterest: mask,
                                          callback: { _, type, event, context in
            if let context = context {
                Unmanaged<ModifierTapMonitor>.fromOpaque(context).takeUnretainedValue().handle(type, event: event)
            }
            return Unmanaged.passUnretained(event)
        }, userInfo: Unmanaged.passUnretained(self).toOpaque()),
              let source = CFMachPortCreateRunLoopSource(nil, tap, 0) else { throw ShortcutError.eventMonitorUnavailable }
        self.tap = tap
        self.source = source
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
    }

    private func handle(_ type: CGEventType, event: CGEvent) {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            gesture.reset()
            if let tap = tap { CGEvent.tapEnable(tap: tap, enable: true) }
        case .flagsChanged:
            if gesture.handle(type: type, keyCode: event.getIntegerValueField(.keyboardEventKeycode),
                              flags: event.flags, time: event.timestamp) {
                action()
            }
        default:
            gesture.reset()
        }
    }

    func reset() { gesture.reset() }

    deinit {
        if let source = source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        if let tap = tap { CFMachPortInvalidate(tap) }
    }
}

private final class ShortcutRegistration {
    let shortcut: Shortcut
    let id: UInt32
    private var hotKeyRef: EventHotKeyRef?
    private var tapMonitor: ModifierTapMonitor?

    init(_ shortcut: Shortcut, id: UInt32, action: @escaping () -> Void) throws {
        self.shortcut = shortcut
        self.id = id
        try shortcut.checkAvailability()
        switch shortcut {
        case .doubleTap(let modifier):
            tapMonitor = try ModifierTapMonitor(modifier: modifier, action: action)
        case .combination(let key, let modifiers):
            guard let code = Shortcut.keyCodes[key] else { throw ShortcutError.invalid }
            let flags = modifiers.reduce(UInt32(0)) { $0 | $1.carbonFlag }
            let hotKeyID = EventHotKeyID(signature: shortcutSignature, id: id)
            var ref: EventHotKeyRef?
            let status = RegisterEventHotKey(UInt32(code), flags, hotKeyID, GetApplicationEventTarget(),
                                             UInt32(kEventHotKeyExclusive), &ref)
            guard status != eventHotKeyExistsErr else { throw ShortcutError.registeredConflict }
            guard status == noErr, let ref = ref else { throw ShortcutError.registrationFailed(status) }
            hotKeyRef = ref
        }
    }

    func resetGesture() { tapMonitor?.reset() }

    deinit {
        if let ref = hotKeyRef { UnregisterEventHotKey(ref) }
    }
}

private let shortcutSignature: OSType = 0x4457_4E53

// MARK: - 快捷键设置窗口

private final class ShortcutEditor: NSWindowController, NSWindowDelegate {
    private let mode = NSPopUpButton()
    private let key = NSPopUpButton()
    private let doubleTapKey = NSPopUpButton()
    private let modifierButtons: [NSButton]
    private let combinationControls = NSStackView()
    private let doubleTapControls = NSStackView()
    private let feedback = NSTextField(wrappingLabelWithString: "")
    private let permissionButton = NSButton(title: "授权输入监控…", target: nil, action: nil)
    private let saveButton = NSButton(title: "保存", target: nil, action: nil)
    private let initialProblem: ShortcutError?
    private let check: (Shortcut) throws -> Void
    private let save: (Shortcut) throws -> Void
    private let didClose: () -> Void

    init(current: Shortcut?, configured: Shortcut?, problem: ShortcutError?,
         check: @escaping (Shortcut) throws -> Void, save: @escaping (Shortcut) throws -> Void,
         didClose: @escaping () -> Void) {
        self.check = check
        self.save = save
        self.didClose = didClose
        initialProblem = problem
        modifierButtons = ShortcutModifier.allCases.map { NSButton(checkboxWithTitle: $0.title, target: nil, action: nil) }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 320),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "更改搜索快捷键"
        window.delegate = self
        window.center()
        window.autorecalculatesKeyViewLoop = true

        mode.addItems(withTitles: ["组合键", "双击修饰键"])
        doubleTapKey.addItems(withTitles: ShortcutModifier.allCases.map(\.title))
        let orderedKeys = Shortcut.keyCodes.keys.sorted {
            Shortcut.keyTitle($0).localizedStandardCompare(Shortcut.keyTitle($1)) == .orderedAscending
        }
        for name in orderedKeys {
            key.addItem(withTitle: Shortcut.keyTitle(name))
            key.lastItem?.representedObject = name
        }
        for control in [mode, key, doubleTapKey] + modifierButtons {
            control.target = self
            control.action = #selector(selectionChanged)
        }
        var selectedModifiers: Set<ShortcutModifier> = [.control]
        var selectedKey = "q"
        doubleTapKey.selectItem(withTitle: ShortcutModifier.option.title)
        if case .combination(let key, let modifiers) = configured ?? current {
            selectedKey = key
            selectedModifiers = modifiers
        } else if case .doubleTap(let modifier) = configured ?? current {
            mode.selectItem(at: 1)
            doubleTapKey.selectItem(withTitle: modifier.title)
        }
        key.selectItem(withTitle: Shortcut.keyTitle(selectedKey))
        for (modifier, button) in zip(ShortcutModifier.allCases, modifierButtons) {
            button.state = selectedModifiers.contains(modifier) ? .on : .off
        }

        let modeRow = NSStackView(views: [NSTextField(labelWithString: "触发方式"), mode])
        let keyRow = NSStackView(views: [NSTextField(labelWithString: "普通键"), key])
        combinationControls.orientation = .vertical
        combinationControls.alignment = .leading
        combinationControls.spacing = 8
        combinationControls.addArrangedSubview(NSStackView(views: modifierButtons))
        combinationControls.addArrangedSubview(keyRow)
        doubleTapControls.addArrangedSubview(NSTextField(labelWithString: "双击按键"))
        doubleTapControls.addArrangedSubview(doubleTapKey)

        let currentLabel = NSTextField(labelWithString: "当前生效快捷键：\(current?.title ?? "无")")
        currentLabel.textColor = .secondaryLabelColor
        let form = NSStackView(views: [currentLabel])
        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = 10
        if let problem = problem {
            let title = configured.map { "「\($0.title)」未生效。 " } ?? ""
            let warning = NSTextField(wrappingLabelWithString: title + problem.localizedDescription)
            warning.textColor = .systemRed
            warning.preferredMaxLayoutWidth = 440
            form.addArrangedSubview(warning)
        }
        feedback.preferredMaxLayoutWidth = 440
        for row in [modeRow, combinationControls, doubleTapControls, feedback] {
            form.addArrangedSubview(row)
        }

        permissionButton.target = self
        permissionButton.action = #selector(requestInputMonitoring)
        let cancelButton = NSButton(title: "取消", target: self, action: #selector(cancel))
        cancelButton.keyEquivalent = "\u{1B}"
        saveButton.target = self
        saveButton.action = #selector(saveSelection)
        saveButton.keyEquivalent = "\r"
        let buttons = NSStackView(views: [cancelButton, saveButton])
        let content = window.contentView!
        for view in [form, buttons, permissionButton] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            form.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            form.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            form.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            buttons.trailingAnchor.constraint(equalTo: form.trailingAnchor),
            buttons.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18),
            permissionButton.leadingAnchor.constraint(equalTo: form.leadingAnchor),
            permissionButton.centerYAnchor.constraint(equalTo: buttons.centerYAnchor),
        ])
        selectionChanged()
    }

    required init?(coder: NSCoder) { nil }

    private var selection: Shortcut? {
        if mode.indexOfSelectedItem == 1 {
            return .doubleTap(ShortcutModifier.allCases[doubleTapKey.indexOfSelectedItem])
        }
        let modifiers = Set(zip(ShortcutModifier.allCases, modifierButtons).compactMap {
            $0.1.state == .on ? $0.0 : nil
        })
        guard !modifiers.isEmpty, let key = key.selectedItem?.representedObject as? String else { return nil }
        return .combination(key: key, modifiers: modifiers)
    }

    @objc private func selectionChanged() {
        let isDoubleTap = mode.indexOfSelectedItem == 1
        combinationControls.isHidden = isDoubleTap
        doubleTapControls.isHidden = !isDoubleTap
        permissionButton.isHidden = true
        saveButton.isEnabled = false
        feedback.isHidden = false
        feedback.textColor = .secondaryLabelColor
        guard let shortcut = selection else {
            feedback.stringValue = "请至少选择一个修饰键。"
            return
        }
        do {
            try check(shortcut)
            feedback.stringValue = isDoubleTap
                ? "可以启用双击监听。其他应用是否监听同一手势无法自动检测。"
                : "「\(shortcut.title)」可以注册。其他应用的共享注册和按键监听可能无法检测。"
            saveButton.isEnabled = true
        } catch {
            showError(error)
        }
    }

    private func showError(_ error: Error) {
        feedback.stringValue = error.localizedDescription
        feedback.textColor = .systemRed
        let problem = error as? ShortcutError
        feedback.isHidden = problem != nil && problem == initialProblem
        permissionButton.isHidden = problem?.needsInputMonitoring != true
        saveButton.isEnabled = problem == .configRead || problem == .configWrite
    }

    @objc private func requestInputMonitoring() {
        if !CGRequestListenEventAccess(),
           let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent") {
            NSWorkspace.shared.open(url)
        }
        selectionChanged()
    }

    @objc private func saveSelection() {
        guard let shortcut = selection else { return }
        do {
            try save(shortcut)
            close()
        } catch {
            showError(error)
        }
    }

    @objc private func cancel() { close() }

    func windowDidBecomeKey(_ notification: Notification) { selectionChanged() }
    func windowWillClose(_ notification: Notification) { didClose() }
}

// MARK: - 自定义网址管理

private struct WebShortcut {
    let title: String
    let keyword: String
    let url: String

    static func validURL(_ url: String) -> Bool {
        guard url.range(of: #"[\s\x00-\x1f\x7f\\]|%(?![0-9a-fA-F]{2})"#, options: .regularExpression) == nil,
              let separator = url.range(of: "://") else { return false }
        let authority = url[separator.upperBound...].prefix { !"/?#".contains($0) }
        let expanded = url.replacingOccurrences(of: "{query}", with: "example")
        guard !authority.contains("{"), !expanded.contains("{"), !expanded.contains("}"),
              let parts = URLComponents(string: expanded),
              let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.url != nil else { return false }
        return parts.port.map { (1...65535).contains($0) } ?? true
    }
}

private final class WebShortcutEditor: NSWindowController, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private let table = NSTableView()
    private let editButton = NSButton(title: "编辑…", target: nil, action: nil)
    private let deleteButton = NSButton(title: "删除…", target: nil, action: nil)
    private let status = NSTextField(labelWithString: "")
    private var entries: [Any]
    private let didSave: () -> Void
    private let didClose: () -> Void

    init(entries: [Any], didSave: @escaping () -> Void, didClose: @escaping () -> Void) {
        self.entries = entries
        self.didSave = didSave
        self.didClose = didClose
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 360),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "自定义网址"
        window.contentMinSize = NSSize(width: 560, height: 280)
        window.delegate = self
        window.center()
        window.autorecalculatesKeyViewLoop = true

        let hint = NSTextField(labelWithString: "在「全部」中输入关键词，按回车用系统默认浏览器打开。")
        hint.textColor = .secondaryLabelColor
        for (key, title, width) in [("title", "标题", 155.0), ("keyword", "关键词", 110.0), ("url", "网址", 318.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key))
            column.title = title
            column.width = width
            table.addTableColumn(column)
        }
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.usesAlternatingRowBackgroundColors = true
        table.rowHeight = 26
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(editClicked)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        scroll.documentView = table

        let addButton = NSButton(title: "新增…", target: self, action: #selector(addClicked))
        editButton.target = self
        editButton.action = #selector(editClicked)
        deleteButton.target = self
        deleteButton.action = #selector(deleteClicked)
        let buttons = NSStackView(views: [addButton, editButton, deleteButton])
        status.textColor = .secondaryLabelColor
        let content = window.contentView!
        for view in [hint, scroll, buttons, status] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            hint.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            hint.leadingAnchor.constraint(equalTo: scroll.leadingAnchor),
            scroll.topAnchor.constraint(equalTo: hint.bottomAnchor, constant: 12),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            scroll.bottomAnchor.constraint(equalTo: buttons.topAnchor, constant: -14),
            buttons.leadingAnchor.constraint(equalTo: scroll.leadingAnchor),
            buttons.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            status.centerYAnchor.constraint(equalTo: buttons.centerYAnchor),
            status.trailingAnchor.constraint(equalTo: scroll.trailingAnchor),
        ])
        window.initialFirstResponder = entries.isEmpty ? addButton : table
        updateControls()
    }

    required init?(coder: NSCoder) { nil }

    func windowWillClose(_ notification: Notification) { didClose() }
    func numberOfRows(in tableView: NSTableView) -> Int { entries.count }

    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row: Int) -> NSView? {
        guard let key = column?.identifier.rawValue else { return nil }
        let entry = entries[row] as? [String: Any]
        let value = entry?[key] as? String ?? (key == "title" ? "无效条目" : "")
        let label = NSTextField(labelWithString: value)
        label.lineBreakMode = .byTruncatingTail
        label.toolTip = value
        return label
    }

    func tableViewSelectionDidChange(_ notification: Notification) { updateControls() }

    private func updateControls() {
        editButton.isEnabled = table.selectedRow >= 0
        deleteButton.isEnabled = editButton.isEnabled
        status.stringValue = entries.isEmpty ? "尚未添加网址" : "共 \(entries.count) 条"
    }

    @objc private func addClicked() { editEntry(at: nil) }

    @objc private func editClicked() {
        if table.selectedRow >= 0 { editEntry(at: table.selectedRow) }
    }

    private func editEntry(at index: Int?, draft: WebShortcut? = nil) {
        guard let window = window else { return }
        let entry = index.flatMap { entries[$0] as? [String: Any] } ?? [:]
        let title = NSTextField(string: draft?.title ?? entry["title"] as? String ?? "")
        let keyword = NSTextField(string: draft?.keyword ?? entry["keyword"] as? String ?? "")
        let url = NSTextField(string: draft?.url ?? entry["url"] as? String ?? "")
        keyword.placeholderString = "例如 gh"
        url.placeholderString = "https://example.com/search?q={query}"
        let fields = NSGridView(views: [
            [NSTextField(labelWithString: "标题"), title],
            [NSTextField(labelWithString: "关键词"), keyword],
            [NSTextField(labelWithString: "网址"), url],
        ])
        fields.frame = NSRect(x: 0, y: 0, width: 400, height: 82)
        fields.column(at: 0).xPlacement = .trailing
        fields.column(at: 1).xPlacement = .fill
        fields.rowSpacing = 8
        let alert = NSAlert()
        alert.messageText = index == nil ? "新增自定义网址" : "编辑自定义网址"
        alert.informativeText = "网址可填固定地址，也可用 {query} 表示搜索词。"
        alert.accessoryView = fields
        let save = alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消").keyEquivalent = "\u{1B}"
        title.setAccessibilityLabel("标题")
        keyword.setAccessibilityLabel("关键词")
        url.setAccessibilityLabel("网址")
        title.nextKeyView = keyword
        keyword.nextKeyView = url
        url.nextKeyView = save
        alert.window.initialFirstResponder = title
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self = self, response == .alertFirstButtonReturn else { return }
            let shortcut = WebShortcut(title: title.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
                                       keyword: keyword.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
                                       url: url.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
            if let message = self.validationError(for: shortcut, excluding: index) {
                self.showMessage(message) { [weak self] in
                    // 保留填写内容，让用户只需修改出错的字段。
                    self?.editEntry(at: index, draft: shortcut)
                }
                return
            }
            var updated = self.entries
            var savedEntry = entry
            savedEntry["title"] = shortcut.title
            savedEntry["keyword"] = shortcut.keyword
            savedEntry["url"] = shortcut.url
            if let index = index { updated[index] = savedEntry } else { updated.append(savedEntry) }
            self.saveEntries(updated, select: index ?? updated.count - 1) { [weak self] in
                self?.editEntry(at: index, draft: shortcut)
            }
        }
    }

    private func validationError(for shortcut: WebShortcut, excluding index: Int?) -> String? {
        if shortcut.title.isEmpty { return "请输入标题。" }
        if shortcut.keyword.isEmpty || shortcut.keyword.contains(where: { $0.isWhitespace }) {
            return "请输入一个不含空格的关键词。"
        }
        for (row, entry) in entries.enumerated() where row != index {
            if let keyword = (entry as? [String: Any])?["keyword"] as? String,
               keyword.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == shortcut.keyword.lowercased() {
                return "关键词「\(shortcut.keyword)」已被使用。"
            }
        }
        if !WebShortcut.validURL(shortcut.url) {
            return "请输入有效的 HTTP 或 HTTPS 网址。{query} 可以写在网址路径、查询参数或片段中。"
        }
        return nil
    }

    @objc private func deleteClicked() {
        let row = table.selectedRow
        guard row >= 0, let window = window else { return }
        let title = (entries[row] as? [String: Any])?["title"] as? String ?? "无效条目"
        let alert = NSAlert()
        alert.messageText = "删除「\(title)」？"
        alert.addButton(withTitle: "取消").keyEquivalent = "\u{1B}"
        alert.addButton(withTitle: "删除")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self = self, response == .alertSecondButtonReturn else { return }
            var updated = self.entries
            updated.remove(at: row)
            self.saveEntries(updated, select: min(row, updated.count - 1))
        }
    }

    private func saveEntries(_ updated: [Any], select row: Int, onFailure: (() -> Void)? = nil) {
        do {
            var config = try readConfig()
            let stored = config["web_shortcuts"] as? [Any] ?? []
            guard NSArray(array: stored).isEqual(to: entries) else {
                entries = stored
                table.reloadData()
                updateControls()
                didSave()
                showMessage("自定义网址已在其他地方修改，列表已更新。请重新选择条目后再试。")
                return
            }
            config["web_shortcuts"] = updated
            try writeConfig(config)
        } catch {
            showMessage("无法保存自定义网址。请检查设置文件的格式和写入权限后再试。", didDismiss: onFailure)
            return
        }
        entries = updated
        table.reloadData()
        if row >= 0 {
            table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            table.scrollRowToVisible(row)
        }
        updateControls()
        didSave()
    }

    private func showMessage(_ message: String, didDismiss: (() -> Void)? = nil) {
        guard let window = window else { return }
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window) { _ in didDismiss?() }
    }
}

// MARK: - App

final class SimpleSearchAppDelegate: NSObject, NSApplicationDelegate {
    fileprivate static var shared: SimpleSearchAppDelegate?
    var controller: SearchController!
    var statusItem: NSStatusItem!
    private var registration: ShortcutRegistration?
    private var configuredShortcut: Shortcut?
    private var hotkeyProblem: ShortcutError?
    private var handler: EventHandlerRef?
    private var nextShortcutID: UInt32 = 0
    private var shortcutEditor: ShortcutEditor?
    private var webShortcutEditor: WebShortcutEditor?
    var warningItem: NSMenuItem?
    var openItem: NSMenuItem?
    let agentPath = homeDir + "/Library/LaunchAgents/" + appIdentifier + ".plist"

    func applicationDidFinishLaunching(_ notification: Notification) {
        SimpleSearchAppDelegate.shared = self
        let me = NSRunningApplication.current
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: appIdentifier)
            .filter { $0.processIdentifier != me.processIdentifier }
        if !others.isEmpty {
            NSApp.terminate(nil)
            return
        }
        buildMainMenu()
        controller = SearchController()
        loadConfiguredShortcut()
        configureLoginItem()
        buildStatusItem()
        updateHotkeyUI()
        if hotkeyProblem != nil {
            DispatchQueue.main.async { [weak self] in self?.editShortcut() }
        } else if ProcessInfo.processInfo.environment["SIMPLE_SEARCH_AT_LOGIN"] == nil {
            controller.show()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openSearch()
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        registration = nil
        if let handler = handler { RemoveEventHandler(handler) }
        controller?.backend.stop()
    }

    private func loadConfiguredShortcut() {
        configuredShortcut = nil
        do {
            let config: [String: Any]
            do {
                config = try readConfig()
            } catch {
                throw ShortcutError.configRead
            }
            guard let value = (config["hotkey"] ?? Shortcut.defaultValue) as? String,
                  let shortcut = Shortcut(value) else { throw ShortcutError.invalid }
            configuredShortcut = shortcut
            try applyShortcut(shortcut, save: false)
        } catch let error as ShortcutError {
            hotkeyProblem = error
        } catch {
            hotkeyProblem = .configRead
        }
    }

    private func prepareShortcut(_ shortcut: Shortcut) throws -> ShortcutRegistration {
        if let registration = registration, shortcut == registration.shortcut {
            try shortcut.checkAvailability()
            return registration
        }
        if case .combination = shortcut, handler == nil {
            var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            let status = InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
                guard let event = event else { return OSStatus(eventNotHandledErr) }
                var hotkey = EventHotKeyID()
                let status = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                               nil, MemoryLayout<EventHotKeyID>.size, nil, &hotkey)
                guard status == noErr, hotkey.signature == shortcutSignature else {
                    return OSStatus(eventNotHandledErr)
                }
                SimpleSearchAppDelegate.shared?.fireShortcut(id: hotkey.id)
                return noErr
            }, 1, &eventType, nil, &handler)
            guard status == noErr else { throw ShortcutError.registrationFailed(status) }
        }
        nextShortcutID &+= 1
        let id = nextShortcutID
        return try ShortcutRegistration(shortcut, id: id) { [weak self] in
            self?.fireShortcut(id: id)
        }
    }

    private func applyShortcut(_ shortcut: Shortcut, save: Bool) throws {
        let candidate = try prepareShortcut(shortcut)
        if save {
            var config: [String: Any]
            do {
                config = try readConfig()
            } catch {
                throw ShortcutError.configRead
            }
            config["hotkey"] = shortcut.configValue
            do {
                try writeConfig(config)
            } catch {
                throw ShortcutError.configWrite
            }
        }
        registration = candidate
        configuredShortcut = shortcut
        hotkeyProblem = nil
    }

    private func fireShortcut(id: UInt32) {
        guard shortcutEditor == nil, registration?.id == id else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self = self, self.shortcutEditor == nil, self.registration?.id == id else { return }
            if self.webShortcutEditor != nil {
                self.openSearch()
            } else {
                self.controller.toggle()
            }
        }
    }

    private func updateHotkeyUI() {
        openItem?.title = registration.map { "打开搜索（\($0.shortcut.title)）" } ?? "打开搜索"
        if hotkeyProblem != nil {
            warningItem?.title = "快捷键设置未生效，点此更换…"
            warningItem?.isHidden = false
        } else {
            warningItem?.isHidden = true
        }
    }

    @objc private func editShortcut() {
        if shortcutEditor == nil {
            if controller.panel.isVisible { controller.hide() }
            shortcutEditor = ShortcutEditor(current: registration?.shortcut, configured: configuredShortcut, problem: hotkeyProblem,
                check: { [weak self] shortcut in
                    _ = try self?.prepareShortcut(shortcut)
                },
                save: { [weak self] shortcut in
                    try self?.applyShortcut(shortcut, save: true)
                    self?.updateHotkeyUI()
                },
                didClose: { [weak self] in
                    self?.registration?.resetGesture()
                    self?.shortcutEditor = nil
                })
        }
        NSApp.activate(ignoringOtherApps: true)
        shortcutEditor?.showWindow(nil)
    }

    @objc func reloadSettings() {
        controller.reloadWebShortcuts()
        webShortcutEditor?.close()
        loadConfiguredShortcut()
        updateHotkeyUI()
        if hotkeyProblem != nil || shortcutEditor != nil {
            shortcutEditor?.close()
            if hotkeyProblem != nil { editShortcut() }
        } else {
            controller.show()
        }
    }

    private func buildMainMenu() {
        // An accessory app still needs an Edit menu for ⌘V / ⌘A / ⌘X / ⌘Z in the search field.
        let main = NSMenu()
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(NSMenuItem.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let img = Bundle.main.image(forResource: "StatusIcon") {
            img.isTemplate = true
            img.size = NSSize(width: 18, height: 18)
            statusItem.button?.image = img
        } else {
            statusItem.button?.title = "☀︎"
        }
        let menu = NSMenu()
        let warning = NSMenuItem(title: "", action: #selector(editShortcut), keyEquivalent: "")
        warning.target = self
        warning.isHidden = true
        menu.addItem(warning)
        warningItem = warning
        let open = NSMenuItem(title: "打开搜索", action: #selector(openSearch), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        openItem = open
        menu.addItem(NSMenuItem.separator())
        let refresh = NSMenuItem(title: "立即更新索引", action: #selector(refreshIndexes), keyEquivalent: "")
        refresh.target = self
        menu.addItem(refresh)
        let login = NSMenuItem(title: "登录时自动启动", action: #selector(toggleLogin(_:)), keyEquivalent: "")
        login.target = self
        login.state = FileManager.default.fileExists(atPath: agentPath) ? .on : .off
        menu.addItem(login)
        let shortcut = NSMenuItem(title: "更改快捷键…", action: #selector(editShortcut), keyEquivalent: "")
        shortcut.target = self
        menu.addItem(shortcut)
        let websites = NSMenuItem(title: "自定义网址…", action: #selector(editWebShortcuts), keyEquivalent: "")
        websites.target = self
        menu.addItem(websites)
        let settings = NSMenuItem(title: "编辑设置…", action: #selector(openSettings), keyEquivalent: "")
        settings.target = self
        menu.addItem(settings)
        let reload = NSMenuItem(title: "重新载入设置", action: #selector(reloadSettings), keyEquivalent: "")
        reload.target = self
        menu.addItem(reload)
        let help = NSMenuItem(title: "使用说明", action: #selector(openHelp), keyEquivalent: "")
        help.target = self
        menu.addItem(help)
        menu.addItem(NSMenuItem.separator())
        let quit = NSMenuItem(title: "退出 simple-search", action: #selector(quitApp), keyEquivalent: "")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
    }

    @objc func openSearch() {
        if let editor = webShortcutEditor {
            if editor.window?.attachedSheet != nil {
                NSApp.activate(ignoringOtherApps: true)
                editor.showWindow(nil)
                return
            }
            editor.close()
        }
        if shortcutEditor != nil {
            editShortcut()
            return
        }
        controller.show()
    }

    @objc func refreshIndexes() {
        controller.refreshIndexes()
    }

    @objc private func editWebShortcuts() {
        if controller.panel.isVisible { controller.hide() }
        NSApp.activate(ignoringOtherApps: true)
        if webShortcutEditor == nil {
            do {
                let entries = try readConfig()["web_shortcuts"] as? [Any] ?? []
                webShortcutEditor = WebShortcutEditor(entries: entries, didSave: { [weak self] in
                    self?.controller.reloadWebShortcuts()
                }, didClose: { [weak self] in
                    self?.registration?.resetGesture()
                    self?.webShortcutEditor = nil
                })
            } catch {
                let alert = NSAlert()
                alert.messageText = "无法读取设置"
                alert.informativeText = "请检查设置文件的格式和读取权限后再试。"
                alert.addButton(withTitle: "好")
                alert.runModal()
            }
        }
        webShortcutEditor?.showWindow(nil)
    }

    @objc func toggleLogin(_ sender: NSMenuItem) {
        let on = sender.state != .on
        setLaunchAtLogin(on)
        sender.state = on ? .on : .off
    }

    @objc func openSettings() {
        let path = configURL.path
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: "{\n  \"hotkey\": \"alt+space\"\n}\n".data(using: .utf8), attributes: nil)
        }
        NSWorkspace.shared.open(configURL)
    }

    @objc func openHelp() {
        NSWorkspace.shared.open(URL(fileURLWithPath: dataDir + "/app/SOP.md"))
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }

    private func configureLoginItem() {
        let key = "loginItemConfigured"
        let defaults = UserDefaults.standard
        let files = FileManager.default
        if appIdentifier == desktopAppIdentifier {
            let legacyID = "local.simple.search"
            let destination = URL(fileURLWithPath: agentPath)
            let legacy = destination.deletingLastPathComponent().appendingPathComponent(legacyID + ".plist")
            if files.fileExists(atPath: legacy.path) {
                do {
                    if !files.fileExists(atPath: agentPath) {
                        let data = try Data(contentsOf: legacy)
                        guard var plist = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
                              plist["Label"] as? String == legacyID, let executable = Bundle.main.executablePath else {
                            throw CocoaError(.fileReadCorruptFile)
                        }
                        let oldArguments = plist["ProgramArguments"] as? [String] ?? []
                        plist["Label"] = appIdentifier
                        plist["ProgramArguments"] = [executable] + oldArguments.dropFirst()
                        let migrated = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
                        // 新启动项写入成功后再移除旧文件，以免迁移失败时丢失启动设置。
                        try migrated.write(to: destination, options: .atomic)
                    }
                    try files.removeItem(at: legacy)
                    defaults.set(true, forKey: key)
                } catch {
                    NSLog("无法迁移 Simple Search 登录启动项：%@", error.localizedDescription)
                }
                return
            }
            if UserDefaults(suiteName: legacyID)?.bool(forKey: key) == true {
                // 旧版已配置但没有启动项，表示用户曾关闭登录启动。
                defaults.set(true, forKey: key)
            }
        }

        if !defaults.bool(forKey: key) {
            if !files.fileExists(atPath: agentPath) { setLaunchAtLogin(true) }
            defaults.set(true, forKey: key)
        }
    }

    func setLaunchAtLogin(_ on: Bool) {
        if on {
            var environment = ["SIMPLE_SEARCH_AT_LOGIN": "1"]
            if !customDataDir.isEmpty { environment["SIMPLE_SEARCH_HOME"] = dataDir }
            let plist: [String: Any] = [
                "Label": appIdentifier,
                "ProgramArguments": [Bundle.main.executablePath ?? ""],
                "RunAtLoad": true,
                "LimitLoadToSessionType": "Aqua",
                "ProcessType": "Interactive",
                "EnvironmentVariables": environment,
            ]
            try? FileManager.default.createDirectory(atPath: homeDir + "/Library/LaunchAgents", withIntermediateDirectories: true, attributes: nil)
            if let data = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0) {
                FileManager.default.createFile(atPath: agentPath, contents: data, attributes: nil)
            }
        } else {
            try? FileManager.default.removeItem(atPath: agentPath)
        }
    }
}

let application = NSApplication.shared
let appDelegate = SimpleSearchAppDelegate()
application.delegate = appDelegate
application.setActivationPolicy(.accessory)
application.run()
