import SwiftUI

/// Shows the markdown the server writes (day journal, week summary, people, timeline): headings, bullets,
/// quotes and paragraphs, with bold, italic and `code` inside a line. Text can be selected and copied.
struct MarkdownView: View {
    var text: String

    private enum Block {
        case heading(Int, String)
        case bullet(Int, String)
        case quote(String)
        case paragraph(String)
    }

    private var blocks: [Block] {
        var out: [Block] = []
        var para: [String] = []
        func flush() {
            if !para.isEmpty { out.append(.paragraph(para.joined(separator: " "))) }
            para = []
        }
        for raw in text.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            let indent = raw.prefix { $0 == " " }.count / 2
            if line.isEmpty {
                flush()
            } else if let level = ["### ": 3, "## ": 2, "# ": 1].first(where: { line.hasPrefix($0.key) }) {
                flush()
                out.append(.heading(level.value, String(line.dropFirst(level.key.count))))
            } else if line.hasPrefix("- ") || line.hasPrefix("* ") {
                flush()
                out.append(.bullet(indent, String(line.dropFirst(2))))
            } else if line.hasPrefix(">") {
                flush()
                out.append(.quote(String(line.dropFirst()).trimmingCharacters(in: .whitespaces)))
            } else {
                para.append(line)
            }
        }
        flush()
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case let .heading(level, text):
                    inline(text)
                        .font(level == 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
                        .padding(.top, level == 1 ? 4 : 8)
                case let .bullet(indent, text):
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").foregroundStyle(.secondary)
                        inline(text)
                    }
                    .padding(.leading, CGFloat(indent) * 16)
                case let .quote(text):
                    inline(text)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 10)
                        .overlay(alignment: .leading) {
                            Rectangle().fill(Color.secondary.opacity(0.3)).frame(width: 3)
                        }
                case let .paragraph(text):
                    inline(text)
                }
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func inline(_ text: String) -> Text {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        if let attributed = try? AttributedString(markdown: text, options: options) {
            return Text(attributed)
        }
        return Text(text)
    }
}
