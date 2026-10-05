import Foundation

enum AppConfig {
    static let appGroup = "group.net.harborvpn"
    static let tunnelBundleID = "net.harborvpn.app.tunnel"
    static let productIDs = ["harbor.monthly", "harbor.yearly"]

    /// "$(AppIdentifierPrefix)net.harborvpn.shared", expanded at build time.
    static var keychainGroup: String {
        Bundle.main.object(forInfoDictionaryKey: "HarborKeychainGroup") as? String ?? ""
    }

    static var apiURL: URL {
        let raw = Bundle.main.object(forInfoDictionaryKey: "HarborAPIURL") as? String ?? ""
        return URL(string: raw) ?? URL(string: "https://api.harborvpn.example")!
    }

    static let privacyPolicyURL = URL(string: "https://harborvpn.example/privacy")!
    static let termsURL = URL(string: "https://harborvpn.example/terms")!
    static let supportURL = URL(string: "https://harborvpn.example/help")!
}

enum TunnelMessage {
    static let stats = "stats"
}
