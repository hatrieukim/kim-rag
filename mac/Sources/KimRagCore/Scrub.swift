import Foundation

/// Che các chuỗi trông giống khóa, mật khẩu hay số thẻ trước khi chữ rời khỏi máy.
/// Cùng bộ luật với `scrub` của kim-rag (src/lifelog/claude_code.py), thêm số thẻ.
public enum Scrub {
    public static let mask = "[đã che]"

    private static let secrets: [NSRegularExpression] = [
        #"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"#,
        #"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}"#,
        #"\bhf_[A-Za-z0-9]{20,}"#,
        #"\bAIza[0-9A-Za-z_-]{30,}"#,
        #"\bgh[pousr]_[A-Za-z0-9]{30,}"#,
        #"\bglpat-[A-Za-z0-9_-]{20,}"#,
        #"\bxox[abprs]-[A-Za-z0-9-]{10,}"#,
        #"\bAKIA[0-9A-Z]{16}\b"#,
        #"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"#,
    ].map { try! NSRegularExpression(pattern: $0, options: [.dotMatchesLineSeparators]) }

    private static let assigned = try! NSRegularExpression(
        pattern: #"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\s*[=:]\s*)(['"]?)[^\s'"]{8,}"#)

    // Mật khẩu truyền thẳng trên dòng lệnh: `mysql -pXXXX`, `sshpass -p XXXX`, `--password XXXX`.
    private static let flag = try! NSRegularExpression(
        pattern: #"(\s(?:-p|--password[= ]|sshpass -p )\s?)(\S{4,})"#)

    private static let card = try! NSRegularExpression(pattern: #"\b\d(?:[ -]?\d){12,18}\b"#)

    public static func clean(_ text: String) -> String {
        var out = text
        for re in secrets {
            out = re.stringByReplacingMatches(in: out, range: NSRange(out.startIndex..., in: out), withTemplate: mask)
        }
        out = assigned.stringByReplacingMatches(
            in: out, range: NSRange(out.startIndex..., in: out), withTemplate: "$1$2\(mask)")
        out = flag.stringByReplacingMatches(
            in: out, range: NSRange(out.startIndex..., in: out), withTemplate: "$1\(mask)")
        return maskCards(out)
    }

    /// Chỉ che dãy số qua được phép thử Luhn, để không che nhầm số hóa đơn hay mã số thuế.
    private static func maskCards(_ text: String) -> String {
        var out = text
        let matches = card.matches(in: text, range: NSRange(text.startIndex..., in: text))
        for m in matches.reversed() {
            guard let r = Range(m.range, in: out) else { continue }
            let digits = out[r].compactMap { $0.wholeNumberValue }
            if digits.count >= 13 && luhn(digits) {
                out.replaceSubrange(r, with: mask)
            }
        }
        return out
    }

    static func luhn(_ digits: [Int]) -> Bool {
        var sum = 0
        for (i, d) in digits.reversed().enumerated() {
            if i % 2 == 1 {
                let x = d * 2
                sum += x > 9 ? x - 9 : x
            } else {
                sum += d
            }
        }
        return sum % 10 == 0
    }
}
