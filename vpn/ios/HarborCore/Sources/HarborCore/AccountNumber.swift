import Foundation

/// Helpers for the 16-digit account number that replaces email and password.
public enum AccountNumber {
    public static let length = 16

    /// Digits only, or nil if the input can't be an account number.
    /// Accepts the spaces and dashes people type or paste.
    public static func normalize(_ input: String) -> String? {
        var digits = ""
        for ch in input {
            if ch.isASCII, ch.isNumber {
                digits.append(ch)
            } else if ch != " " && ch != "-" && !ch.isNewline {
                return nil
            }
        }
        return digits.count == length ? digits : nil
    }

    /// "1234567890123456" -> "1234 5678 9012 3456"
    public static func grouped(_ number: String) -> String {
        var out = ""
        for (i, ch) in number.enumerated() {
            if i > 0, i % 4 == 0 { out.append(" ") }
            out.append(ch)
        }
        return out
    }

    /// "•••• •••• •••• 3456" for display while hidden.
    public static func masked(_ number: String) -> String {
        guard number.count >= 4 else { return number }
        return "•••• •••• •••• " + number.suffix(4)
    }
}
