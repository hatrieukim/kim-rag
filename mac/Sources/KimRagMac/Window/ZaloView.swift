import SwiftUI

/// Zalo chats of the day and the messages of the one selected. Product 10 is in its first round: these are
/// the raw messages; groups that are not allowed are only counted, never read.
struct ZaloView: View {
    @ObservedObject var model: WindowModel

    var body: some View {
        let day = dayString(model.day)
        Loader(key: day, load: { try await model.api.get("/app/day", query: ["day": day]) as DayData }) { data, _ in
            HSplitView {
                Group {
                    if data.zalo.isEmpty {
                        ContentUnavailableView("Không có tin Zalo trong ngày", systemImage: "bubble.left.and.bubble.right")
                    } else {
                        List(data.zalo, selection: $model.openChat) { c in ChatRow(chat: c).tag(c.id) }
                    }
                }
                .frame(minWidth: 240, idealWidth: 300, maxWidth: 400)
                Group {
                    if let chat = model.openChat, let info = data.zalo.first(where: { $0.id == chat }) {
                        ThreadView(model: model, chat: info, day: day)
                    } else {
                        ContentUnavailableView("Chọn một cuộc chat", systemImage: "bubble.left")
                    }
                }
                .frame(minWidth: 400, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}

private struct ChatRow: View {
    var chat: ZaloChat

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: chat.kind == "group" ? "person.3" : "person").foregroundStyle(.secondary).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(chat.name ?? "?").fontWeight(.medium).lineLimit(1)
                Text(chat.skipped ? "nhóm chưa cho phép, chỉ đếm" : "\(chat.messages) tin, \(chat.mine) của Kim")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(clock(chat.last)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                if chat.mentionsMe > 0 {
                    Text("@Kim").font(.caption2.bold()).foregroundStyle(.orange)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

private struct ThreadView: View {
    @ObservedObject var model: WindowModel
    var chat: ZaloChat
    var day: String

    var body: some View {
        Loader(key: "\(chat.id)-\(day)", load: {
            try await model.api.get("/app/zalo/\(chat.id)", query: ["day": day]) as ZaloThread
        }) { thread, _ in
            if chat.skipped {
                ContentUnavailableView {
                    Label(chat.name ?? "Nhóm", systemImage: "eye.slash")
                } description: {
                    Text("Nhóm này chưa nằm trong danh sách cho phép (zalo.json), nên chỉ đếm \(thread.messages.count) tin, không lưu người gửi hay nội dung.")
                }
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(thread.messages) { m in Bubble(message: m, group: chat.kind == "group") }
                    }
                    .padding(16)
                }
            }
        }
    }
}

private struct Bubble: View {
    var message: ZaloThread.Message
    var group: Bool

    var body: some View {
        HStack {
            if message.isSelf { Spacer(minLength: 60) }
            VStack(alignment: message.isSelf ? .trailing : .leading, spacing: 3) {
                if group && !message.isSelf, let name = message.fromName {
                    Text(name).font(.caption.bold()).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 4) {
                    if let quote = message.quote, !quote.isEmpty {
                        Text(quote).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                            .padding(.leading, 6)
                            .overlay(alignment: .leading) { Rectangle().fill(Color.secondary.opacity(0.4)).frame(width: 2) }
                    }
                    Text(message.text ?? "(\(message.msgType ?? "tin"))").textSelection(.enabled)
                }
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 12)
                    .fill(message.isSelf ? Color.accentColor.opacity(0.22) : Color(nsColor: .controlBackgroundColor)))
                Text(clock(message.ts)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            }
            if !message.isSelf { Spacer(minLength: 60) }
        }
    }
}
