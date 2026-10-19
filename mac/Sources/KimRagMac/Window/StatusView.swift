import AppKit
import KimRagCore
import SwiftUI

/// `lifelog doctor` in the window, plus what the pipeline still has to do and what failed.
struct StatusView: View {
    @ObservedObject var model: WindowModel
    @State private var refresh = 0

    var body: some View {
        Loader(key: "\(refresh)", load: { try await model.api.get("/app/status", timeout: 120) as StatusData }) { s, _ in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack {
                        Label(s.ok ? "Mọi thứ cần thiết đều chạy" : "Có phần chưa chạy",
                              systemImage: s.ok ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                            .font(.title2.bold())
                            .foregroundStyle(s.ok ? Color.green : Color.orange)
                        Spacer()
                        Button { refresh += 1 } label: { Label("Kiểm tra lại", systemImage: "arrow.clockwise") }
                    }
                    pipeline(s)
                    checks(s)
                    actions(s)
                }
                .padding(20)
                .frame(maxWidth: 900, alignment: .leading)
            }
        }
    }

    @ViewBuilder private func pipeline(_ s: StatusData) -> some View {
        Card(title: "Hàng đợi xử lý", icon: "gearshape.2") {
            if let error = s.dbError {
                Text(error).foregroundStyle(.orange)
            } else {
                let chunks = s.chunks ?? [:]
                let episodes = s.episodes ?? [:]
                let waitingChunks = chunks.filter { ["received", "vad"].contains($0.key) }.values.reduce(0, +)
                let waitingEpisodes = episodes.filter { !["indexed", "ingested"].contains($0.key) }.values.reduce(0, +)
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                    GridRow { Text("File audio chờ xử lý"); Text("\(waitingChunks)").monospacedDigit() }
                    GridRow { Text("Episode chưa xong"); Text("\(waitingEpisodes)").monospacedDigit() }
                    GridRow { Text("Sự kiện chờ gom"); Text("\(s.waitingEvents ?? 0)").monospacedDigit() }
                    GridRow {
                        Text("Episode theo bước").foregroundStyle(.secondary)
                        Text(episodes.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: " · "))
                            .foregroundStyle(.secondary)
                    }
                }
                ForEach(s.failed ?? []) { f in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(kindName(f.kind)) \("\(dayLabel(f.startedAt)) \(clock(f.startedAt))") · \(f.id.prefix(8)) · thử \(f.attempts) lần")
                            .font(.callout)
                        Text(f.error ?? "").font(.caption).foregroundStyle(.orange).lineLimit(2).textSelection(.enabled)
                    }
                }
            }
        }
    }

    @ViewBuilder private func checks(_ s: StatusData) -> some View {
        let sections = s.checks.reduce(into: [String]()) { acc, c in if !acc.contains(c.section) { acc.append(c.section) } }
        ForEach(sections, id: \.self) { section in
            Card(title: section.isEmpty ? "Máy" : section, icon: "checklist") {
                ForEach(s.notes[section] ?? [], id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
                ForEach(s.checks.filter { $0.section == section }) { c in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: c.good == true ? "checkmark.circle.fill" : c.good == false
                              ? "xmark.circle.fill" : "minus.circle")
                            .foregroundStyle(c.good == true ? Color.green : c.good == false ? Color.red : Color.secondary)
                        Text(c.label).fontWeight(.medium)
                        Text(c.detail).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    .font(.callout)
                }
            }
        }
    }

    private func actions(_ s: StatusData) -> some View {
        HStack {
            Button("Mở file cấu hình") { model.onOpenConfig() }
            Button("Mở thư mục dữ liệu") { NSWorkspace.shared.open(URL(fileURLWithPath: s.dataDir)) }
        }
    }
}
