import AppKit
import SwiftUI

/// The conversation laid out as text, read from Claude Code's transcript (wm's transcript.py). Selecting text offers
/// Reply (quote it in the next message) and Ask on the side (a discussion that stays out of the main chat). Typing
/// here sends to the same Claude as the terminal; permission prompts still need the terminal.
struct ChatView: View {
    @Environment(AppModel.self) private var model
    @State private var draft = ""
    @FocusState private var composerFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .bottom) {
                if let chat = model.chat, chat.key == model.selection {
                    if chat.messages.isEmpty {
                        ContentUnavailableView("No messages yet", systemImage: "text.bubble",
                                               description: Text("Describe the task below, or in the terminal."))
                            .frame(maxHeight: .infinity)
                    } else {
                        TranscriptText(messages: chat.messages) { model.chatSelection = $0 }
                    }
                } else {
                    ProgressView().controlSize(.small).frame(maxHeight: .infinity)
                }
                if let sel = model.chatSelection, !sel.text.isEmpty {
                    SelectionBar(selection: sel)
                        .padding(.bottom, 10)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            if let tid = model.sideThread, let thread = model.brief?.threads.first(where: { $0.id == tid }) {
                QuoteDiscussion(thread: thread)
            }
            Divider()
            StatusStrip()
            composer
        }
        .animation(.snappy(duration: 0.2), value: model.chatSelection)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let quote = model.quote {
                HStack(alignment: .top, spacing: 8) {
                    Rectangle().fill(Color.secondary.opacity(0.4)).frame(width: 3)
                    Text(quote).font(.callout).foregroundStyle(.secondary).lineLimit(3)
                    Spacer()
                    Button("Remove") { model.quote = nil }.buttonStyle(.link).font(.caption)
                }
                .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField(model.quote == nil ? "Message Claude…" : "Ask about the quoted part…", text: $draft,
                          axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...6)
                    .focused($composerFocused)
                    .onSubmit(submit)
                    .padding(8)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
                Button("Send", action: submit)
                    .buttonStyle(.borderedProminent)
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(12)
        .onChange(of: model.quote) { _, quote in if quote != nil { composerFocused = true } }
    }

    private func submit() {
        let text = draft
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        draft = ""
        Task { if await !model.send(text) { draft = text } }
    }
}

/// What the session is doing, between the transcript and the message box.
private struct StatusStrip: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if let ws = model.selected {
            HStack(spacing: 8) {
                switch ws.status {
                case .working:
                    ProgressView().controlSize(.mini)
                    Text("Claude is working").foregroundStyle(.secondary)
                case .needsYou:
                    Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
                    Text("Claude is waiting on a prompt in the terminal").foregroundStyle(.orange)
                    Button("Show Terminal") { model.center = .terminal }.buttonStyle(.link)
                default:
                    EmptyView()
                }
                Spacer()
                if let n = ws.openQuestions, n > 0 {
                    Button("\(n) \(n == 1 ? "question waits" : "questions wait") on you →") {
                        model.panel = .status
                        model.showChanges = true
                    }
                    .buttonStyle(.link)
                    .foregroundStyle(Color.amber)
                }
            }
            .font(.callout)
            .padding(.horizontal, 12)
            .padding(.top, 8)
        }
    }
}

private struct SelectionBar: View {
    @Environment(AppModel.self) private var model
    var selection: ChatSelection

    var body: some View {
        HStack(spacing: 2) {
            Button("Reply") { model.reply(selection) }
            Divider().frame(height: 14)
            Button("Ask on the side") { Task { await model.askOnTheSide(selection) } }
        }
        .buttonStyle(.plain)
        .font(.callout.weight(.medium))
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.regularMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.primary.opacity(0.1)))
        .shadow(color: .black.opacity(0.15), radius: 6, y: 2)
    }
}

/// A side discussion about a quoted part of the chat, docked above the message box.
private struct QuoteDiscussion: View {
    @Environment(AppModel.self) private var model
    var thread: SideThread

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Side question").font(.caption.weight(.semibold))
                Spacer()
                Button("Close") { Task { await model.closeSide(thread.id) } }.buttonStyle(.link).font(.caption)
            }
            if let quote = thread.anchor.quote {
                HStack(alignment: .top, spacing: 8) {
                    Rectangle().fill(Color.secondary.opacity(0.4)).frame(width: 3)
                    Text(quote).font(.callout).foregroundStyle(.secondary).lineLimit(4)
                }
                .fixedSize(horizontal: false, vertical: true)
            }
            ScrollView {
                Discussion(thread: thread, placeholder: "Ask about this part…") { text in
                    await model.sideAsk(thread.id, text)
                }
            }
            .frame(maxHeight: 260)
        }
        .padding(12)
        .background(Color.primary.opacity(0.03))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - The transcript as one selectable text view

/// One NSTextView for the whole conversation: selection works across messages, and each run of text carries the id
/// of the message it came from, so a selection can be quoted with its source.
struct TranscriptText: NSViewRepresentable {
    var messages: [ChatMessage]
    var onSelect: (ChatSelection?) -> Void

    static let messageKey = NSAttributedString.Key("wmMessage")

    func makeCoordinator() -> Coordinator { Coordinator(onSelect: onSelect) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        let text = scroll.documentView as! NSTextView
        text.isEditable = false
        text.isSelectable = true
        text.drawsBackground = false
        text.textContainerInset = NSSize(width: 18, height: 16)
        text.delegate = context.coordinator
        context.coordinator.textView = text
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.onSelect = onSelect
        guard context.coordinator.shown != messages, let text = scroll.documentView as? NSTextView else { return }
        let clip = scroll.contentView
        let atBottom = clip.bounds.maxY >= (text.frame.height - 40)
        context.coordinator.shown = messages
        text.textStorage?.setAttributedString(Self.render(messages))
        if atBottom || context.coordinator.first {
            context.coordinator.first = false
            DispatchQueue.main.async { text.scrollToEndOfDocument(nil) }
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var onSelect: (ChatSelection?) -> Void
        var shown: [ChatMessage] = []
        var first = true
        weak var textView: NSTextView?

        init(onSelect: @escaping (ChatSelection?) -> Void) { self.onSelect = onSelect }

        func textViewDidChangeSelection(_ notification: Notification) {
            guard let text = textView, let storage = text.textStorage else { return }
            let range = text.selectedRange()
            guard range.length > 0 else { onSelect(nil); return }
            let selected = (storage.string as NSString).substring(with: range)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let id = storage.attribute(TranscriptText.messageKey, at: range.location, effectiveRange: nil) as? String
            onSelect(selected.isEmpty ? nil : ChatSelection(text: selected, messageID: id))
        }
    }

    // MARK: Rendering

    static func render(_ messages: [ChatMessage]) -> NSAttributedString {
        let out = NSMutableAttributedString()
        for m in messages {
            let block = NSMutableAttributedString()
            switch m.role {
            case "tools":
                block.append(line("▸ \(m.text)", font: .systemFont(ofSize: 11.5), color: .tertiaryLabelColor))
                if let tools = m.tools, !tools.isEmpty {
                    block.addAttribute(.toolTip, value: tools.joined(separator: "\n"),
                                       range: NSRange(location: 0, length: block.length))
                }
            case "note":
                block.append(line("— \(m.text) —", font: .systemFont(ofSize: 11.5), color: .tertiaryLabelColor))
            default:
                let (label, color): (String, NSColor) = switch m.role {
                case "user": ("You", .controlAccentColor)
                case "wm": ("wm", .secondaryLabelColor)
                default: ("Claude", .secondaryLabelColor)
                }
                block.append(line(label, font: .systemFont(ofSize: 11, weight: .semibold), color: color))
                block.append(body(m.text, dim: m.role == "wm", strong: m.role == "user"))
            }
            block.addAttribute(messageKey, value: m.id, range: NSRange(location: 0, length: block.length))
            let style = NSMutableParagraphStyle()
            style.paragraphSpacing = 4
            block.append(NSAttributedString(string: "\n"))
            out.append(block)
        }
        return out
    }

    private static func line(_ text: String, font: NSFont, color: NSColor) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        style.paragraphSpacingBefore = 6
        style.paragraphSpacing = 2
        return NSAttributedString(string: text + "\n", attributes: [.font: font, .foregroundColor: color,
                                                                    .paragraphStyle: style])
    }

    /// Light markdown: code fences and table rows in monospace, headings in bold, inline bold/italic/code/links.
    private static func body(_ text: String, dim: Bool, strong: Bool) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let base = NSFont.systemFont(ofSize: 13.5, weight: strong ? .medium : .regular)
        let color: NSColor = dim ? .secondaryLabelColor : .labelColor
        let mono = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let style = NSMutableParagraphStyle()
        style.lineSpacing = 2.5
        style.paragraphSpacing = 5
        var inFence = false
        var tableRows: [String] = []
        func flushTable() {
            if !tableRows.isEmpty {
                out.append(table(tableRows, font: base, color: color, mono: mono))
                tableRows = []
            }
        }
        for raw in text.components(separatedBy: "\n") {
            let trimmed = raw.trimmingCharacters(in: .whitespaces)
            if !inFence && trimmed.hasPrefix("|") {
                tableRows.append(trimmed)
                continue
            }
            flushTable()
            if trimmed.hasPrefix("```") {
                inFence.toggle()
                continue
            }
            let attrs: [NSAttributedString.Key: Any] = [.font: base, .foregroundColor: color, .paragraphStyle: style]
            if inFence {
                out.append(NSAttributedString(string: raw + "\n", attributes: attrs.merging([.font: mono]) { $1 }))
                continue
            }
            var lineText = raw
            var font = base
            if let range = raw.range(of: "^#{1,6} ", options: .regularExpression) {
                lineText = String(raw[range.upperBound...])
                font = .systemFont(ofSize: 14, weight: .semibold)
            }
            out.append(inline(lineText, font: font, color: color, mono: mono, style: style))
            out.append(NSAttributedString(string: "\n", attributes: attrs))
        }
        flushTable()
        return out
    }

    /// A markdown table as a real one: bordered cells that wrap, the header row in bold, columns sized by content.
    private static func table(_ lines: [String], font: NSFont, color: NSColor, mono: NSFont) -> NSAttributedString {
        func cells(_ line: String) -> [String] {
            var t = line
            if t.hasPrefix("|") { t.removeFirst() }
            if t.hasSuffix("|") { t.removeLast() }
            return t.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
        }
        let isRule = { (line: String) in line.range(of: #"^\|?(\s*:?-{2,}:?\s*\|)+\s*(:?-{2,}:?\s*)?$"#,
                                                    options: .regularExpression) != nil }
        var rows = lines.filter { !isRule($0) }.map(cells)
        let hasHeader = lines.count > 1 && isRule(lines[1])
        let columns = rows.map(\.count).max() ?? 0
        guard columns > 1 else {  // a lone "|" line isn't a table
            return NSAttributedString(string: lines.joined(separator: "\n") + "\n",
                                      attributes: [.font: mono, .foregroundColor: color])
        }
        rows = rows.map { $0 + Array(repeating: "", count: columns - $0.count) }
        // columns share the width by their longest cell, within limits, so short columns don't get squeezed
        let lengths = (0..<columns).map { c in
            Double(min(max(rows.map { $0[c].count }.max() ?? 1, 6), 60))
        }
        let total = lengths.reduce(0, +)
        let tableFont = NSFont.systemFont(ofSize: font.pointSize - 1)
        let table = NSTextTable()
        table.numberOfColumns = columns
        table.collapsesBorders = true
        table.layoutAlgorithm = .automaticLayoutAlgorithm
        table.setContentWidth(100, type: .percentageValueType)
        let out = NSMutableAttributedString()
        for (r, row) in rows.enumerated() {
            let header = hasHeader && r == 0
            for (c, cell) in row.enumerated() {
                let block = NSTextTableBlock(table: table, startingRow: r, rowSpan: 1, startingColumn: c, columnSpan: 1)
                block.setWidth(0.5, type: .absoluteValueType, for: .border)
                block.setBorderColor(.separatorColor)
                block.setWidth(5, type: .absoluteValueType, for: .padding)
                block.setValue(CGFloat(lengths[c] / total * 100), type: .percentageValueType, for: .width)
                if header { block.backgroundColor = NSColor.quaternaryLabelColor.withAlphaComponent(0.15) }
                let style = NSMutableParagraphStyle()
                style.textBlocks = [block]
                style.lineSpacing = 1.5
                let f = header ? NSFont.systemFont(ofSize: tableFont.pointSize, weight: .semibold) : tableFont
                out.append(inline(cell.isEmpty ? " " : cell, font: f, color: color, mono: mono, style: style))
                out.append(NSAttributedString(string: "\n", attributes: [.font: f, .paragraphStyle: style]))
            }
        }
        let after = NSMutableParagraphStyle()
        after.paragraphSpacingBefore = 4
        out.append(NSAttributedString(string: "\n", attributes: [.font: NSFont.systemFont(ofSize: 4), .paragraphStyle: after]))
        return out
    }

    private static func inline(_ text: String, font: NSFont, color: NSColor, mono: NSFont,
                               style: NSParagraphStyle) -> NSAttributedString {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard let parsed = try? AttributedString(markdown: text, options: opts) else {
            return NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color,
                                                                 .paragraphStyle: style])
        }
        let out = NSMutableAttributedString()
        for run in parsed.runs {
            let piece = String(parsed[run.range].characters)
            var f = font
            let intent = run.inlinePresentationIntent ?? []
            if intent.contains(.code) {
                f = mono
            } else if intent.contains(.stronglyEmphasized) {
                f = NSFont.systemFont(ofSize: font.pointSize, weight: .semibold)
            } else if intent.contains(.emphasized) {
                f = NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask)
            }
            var attrs: [NSAttributedString.Key: Any] = [.font: f, .foregroundColor: color, .paragraphStyle: style]
            if intent.contains(.code) { attrs[.backgroundColor] = NSColor.quaternaryLabelColor.withAlphaComponent(0.3) }
            if let link = run.link { attrs[.link] = link }
            out.append(NSAttributedString(string: piece, attributes: attrs))
        }
        return out
    }
}
