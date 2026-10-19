import Foundation

/// Lệnh terminal do hook zsh (mac/kim-rag.zsh) ghi ra file, mỗi lệnh một dòng:
/// `giờ bắt đầu (epoch) TAB số giây chạy TAB mã thoát TAB thư mục TAB lệnh`.
/// Lệnh đã được hook thay tab, xuống dòng và gạch chéo ngược bằng `\t`, `\n`, `\\`.
public struct ShellCommand: Equatable {
    public var startedAt: Date
    public var seconds: Double
    public var status: Int
    public var cwd: String
    public var command: String
}

public enum ShellSpool {
    public static func parse(_ line: Substring) -> ShellCommand? {
        let parts = line.split(separator: "\t", maxSplits: 4, omittingEmptySubsequences: false)
        guard parts.count == 5, let t0 = Double(parts[0]), let secs = Double(parts[1]), let status = Int(parts[2]) else {
            return nil
        }
        let command = unescape(parts[4])
        guard !command.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return ShellCommand(startedAt: Date(timeIntervalSince1970: t0), seconds: max(0, secs), status: status,
                            cwd: String(parts[3]), command: command)
    }

    static func unescape(_ s: Substring) -> String {
        var out = ""
        var escaping = false
        for ch in s {
            if escaping {
                switch ch {
                case "t": out.append("\t")
                case "n": out.append("\n")
                default: out.append(ch)
                }
                escaping = false
            } else if ch == "\\" {
                escaping = true
            } else {
                out.append(ch)
            }
        }
        if escaping { out.append("\\") }
        return out
    }

    /// Lấy các lệnh đã ghi. Đổi tên file trước rồi mới đọc ở lần gọi sau, để zsh luôn ghi vào file mới
    /// và không có dòng nào bị đọc dở.
    public static func take(from url: URL) -> [ShellCommand] {
        let fm = FileManager.default
        let reading = url.appendingPathExtension("reading")
        var out: [ShellCommand] = []
        if let text = try? String(contentsOf: reading, encoding: .utf8) {
            out = text.split(separator: "\n").compactMap(parse)
            try? fm.removeItem(at: reading)
        }
        if fm.fileExists(atPath: url.path) && !fm.fileExists(atPath: reading.path) {
            try? fm.moveItem(at: url, to: reading)
        }
        return out
    }
}
