import Foundation

/// Everything the packet tunnel needs, handed over in the VPN profile's
/// `providerConfiguration`. The private key is not in here; it lives in the
/// shared Keychain and is referenced by `passwordReference`.
public struct TunnelSpec: Codable, Equatable, Sendable {
    public var serverID: String
    public var serverPublicKey: String
    /// host:port; IPv6 hosts in brackets.
    public var endpoint: String
    public var addresses: [String]
    public var dns: [String]
    public var mtu: Int
    public var locationName: String
    public var countryCode: String

    public init(serverID: String, serverPublicKey: String, endpoint: String, addresses: [String],
                dns: [String], mtu: Int = 1280, locationName: String, countryCode: String) {
        self.serverID = serverID
        self.serverPublicKey = serverPublicKey
        self.endpoint = endpoint
        self.addresses = addresses
        self.dns = dns
        self.mtu = mtu
        self.locationName = locationName
        self.countryCode = countryCode
    }

    public init(server: Server, device: DeviceInfo, dns: [String]) {
        self.init(serverID: server.id, serverPublicKey: server.publicKey,
                  endpoint: "\(server.ipv4):\(server.port)",
                  addresses: [device.ipv4Address, device.ipv6Address], dns: dns,
                  locationName: "\(server.city), \(server.country)", countryCode: server.countryCode)
    }

    public static let providerKey = "spec"

    public func providerConfiguration() throws -> [String: Any] {
        let data = try JSONEncoder().encode(self)
        return [Self.providerKey: data]
    }

    public init?(providerConfiguration: [String: Any]?) {
        guard let data = providerConfiguration?[Self.providerKey] as? Data,
              let spec = try? JSONDecoder().decode(TunnelSpec.self, from: data) else { return nil }
        self = spec
    }

    /// A wg-quick style config for the diagnostics screen, without the key.
    public func redactedConfig() -> String {
        """
        [Interface]
        PrivateKey = (stored in Keychain)
        Address = \(addresses.joined(separator: ", "))
        DNS = \(dns.joined(separator: ", "))
        MTU = \(mtu)

        [Peer]
        PublicKey = \(serverPublicKey)
        Endpoint = \(endpoint)
        AllowedIPs = 0.0.0.0/0, ::/0
        PersistentKeepalive = 25
        """
    }
}

/// Live numbers the tunnel reports back to the app.
public struct TunnelStats: Codable, Equatable, Sendable {
    public var rxBytes: UInt64
    public var txBytes: UInt64
    public var lastHandshake: Date?

    public init(rxBytes: UInt64 = 0, txBytes: UInt64 = 0, lastHandshake: Date? = nil) {
        self.rxBytes = rxBytes
        self.txBytes = txBytes
        self.lastHandshake = lastHandshake
    }

    /// Parses the `wg` UAPI "get" output the Go bridge returns.
    public static func parse(uapi: String) -> TunnelStats {
        var stats = TunnelStats()
        var newest: Double = 0
        for line in uapi.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1)
            guard parts.count == 2 else { continue }
            let value = String(parts[1])
            switch parts[0] {
            case "rx_bytes": stats.rxBytes += UInt64(value) ?? 0
            case "tx_bytes": stats.txBytes += UInt64(value) ?? 0
            case "last_handshake_time_sec": newest = max(newest, Double(value) ?? 0)
            default: break
            }
        }
        if newest > 0 { stats.lastHandshake = Date(timeIntervalSince1970: newest) }
        return stats
    }
}

public enum Format {
    /// 1536 -> "1.5 KB". Decimal units, like Settings > Cellular.
    public static func bytes(_ n: UInt64) -> String {
        let units = ["B", "KB", "MB", "GB", "TB"]
        var value = Double(n)
        var i = 0
        while value >= 1000, i < units.count - 1 {
            value /= 1000
            i += 1
        }
        if i == 0 { return "\(n) B" }
        let rounded = value < 10 ? (value * 10).rounded() / 10 : value.rounded()
        let text = rounded == rounded.rounded() ? String(Int(rounded)) : String(rounded)
        return "\(text) \(units[i])"
    }

    /// 3725 -> "1:02:05", 65 -> "01:05".
    public static func duration(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
        func pad(_ v: Int) -> String { v < 10 ? "0\(v)" : "\(v)" }
        return h > 0 ? "\(h):\(pad(m)):\(pad(sec))" : "\(pad(m)):\(pad(sec))"
    }
}
