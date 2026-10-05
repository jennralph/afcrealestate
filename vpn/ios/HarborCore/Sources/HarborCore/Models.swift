import Foundation

/// A VPN server as published by `GET /v1/servers`.
public struct Server: Codable, Hashable, Identifiable, Sendable {
    public let id: String
    public let countryCode: String
    public let country: String
    public let city: String
    public let latitude: Double
    public let longitude: Double
    public let hostname: String
    public let ipv4: String
    public let ipv6: String?
    public let port: Int
    public let publicKey: String
    public let free: Bool
    public let features: [String]
    /// 0–100.
    public let load: Int

    public init(id: String, countryCode: String, country: String, city: String,
                latitude: Double = 0, longitude: Double = 0, hostname: String = "",
                ipv4: String, ipv6: String? = nil, port: Int = 51820, publicKey: String,
                free: Bool = false, features: [String] = [], load: Int = 0) {
        self.id = id
        self.countryCode = countryCode
        self.country = country
        self.city = city
        self.latitude = latitude
        self.longitude = longitude
        self.hostname = hostname
        self.ipv4 = ipv4
        self.ipv6 = ipv6
        self.port = port
        self.publicKey = publicKey
        self.free = free
        self.features = features
        self.load = load
    }

    /// Servers in the same city form one user-facing location.
    public var locationID: String { Location.makeID(countryCode: countryCode, city: city) }
}

/// The three resolvers every node runs inside the tunnel.
public struct DNSResolvers: Codable, Hashable, Sendable {
    public let standard: [String]
    public let blockAds: [String]
    public let blockAdsMalware: [String]

    public init(standard: [String], blockAds: [String], blockAdsMalware: [String]) {
        self.standard = standard
        self.blockAds = blockAds
        self.blockAdsMalware = blockAdsMalware
    }

    public func servers(for protection: ThreatProtection) -> [String] {
        switch protection {
        case .off: standard
        case .adsTrackers: blockAds
        case .adsTrackersMalware: blockAdsMalware
        }
    }
}

public struct ServerList: Codable, Hashable, Sendable {
    public let servers: [Server]
    public let dns: DNSResolvers

    public init(servers: [Server], dns: DNSResolvers) {
        self.servers = servers
        self.dns = dns
    }

    /// Countries sorted by name, each with its cities sorted by name.
    public func countries() -> [Country] {
        let byLocation = Dictionary(grouping: servers, by: \.locationID)
        let locations = byLocation.values.compactMap { group -> Location? in
            guard let first = group.first else { return nil }
            return Location(countryCode: first.countryCode, country: first.country, city: first.city,
                            servers: group.sorted { $0.id < $1.id })
        }
        return Dictionary(grouping: locations, by: \.countryCode).values.compactMap { locs in
            guard let first = locs.first else { return nil }
            return Country(code: first.countryCode, name: first.country,
                           locations: locs.sorted { $0.city.localizedCompare($1.city) == .orderedAscending })
        }
        .sorted { $0.name.localizedCompare($1.name) == .orderedAscending }
    }

    public func location(id: String) -> Location? {
        let group = servers.filter { $0.locationID == id }
        guard let first = group.first else { return nil }
        return Location(countryCode: first.countryCode, country: first.country, city: first.city, servers: group)
    }
}

public struct Location: Identifiable, Hashable, Sendable {
    public let countryCode: String
    public let country: String
    public let city: String
    public let servers: [Server]

    public var id: String { Location.makeID(countryCode: countryCode, city: city) }
    public var name: String { "\(city), \(country)" }
    public var flag: String { Flag.emoji(countryCode) }
    /// Free accounts may use this location.
    public var isFree: Bool { servers.contains(where: \.free) }
    public var load: Int { servers.map(\.load).min() ?? 100 }
    public var features: Set<String> { Set(servers.flatMap(\.features)) }

    public static func makeID(countryCode: String, city: String) -> String {
        "\(countryCode.lowercased())-\(city.lowercased().replacingOccurrences(of: " ", with: "-"))"
    }
}

public struct Country: Identifiable, Hashable, Sendable {
    public let code: String
    public let name: String
    public let locations: [Location]
    public var id: String { code }
    public var flag: String { Flag.emoji(code) }
    public var isFree: Bool { locations.contains(where: \.isFree) }
}

public enum Flag {
    /// "se" -> 🇸🇪
    public static func emoji(_ countryCode: String) -> String {
        let base: UInt32 = 0x1F1E6 - 0x41
        var out = ""
        for scalar in countryCode.uppercased().unicodeScalars where ("A"..."Z").contains(scalar) {
            if let flag = UnicodeScalar(base + scalar.value) { out.unicodeScalars.append(flag) }
        }
        return out.isEmpty ? "🌐" : out
    }
}

public enum Tier: String, Codable, Sendable {
    case free, paid
}

public struct DeviceInfo: Codable, Hashable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let publicKey: String
    public let ipv4Address: String
    public let ipv6Address: String
    public let created: Date
    public let keyRotated: Date
}

public struct AccountInfo: Codable, Hashable, Sendable {
    /// Present only in the response that created the account.
    public let number: String?
    public let tier: Tier
    public let paidUntil: Date?
    public let maxDevices: Int
    public let devices: [DeviceInfo]
}
