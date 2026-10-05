import Foundation

public enum AutoConnect: String, Codable, CaseIterable, Sendable {
    case off
    /// Connect on any Wi-Fi network not marked as trusted.
    case untrustedWiFi
    /// Stay connected everywhere except trusted Wi-Fi.
    case always
}

public enum ThreatProtection: String, Codable, CaseIterable, Sendable {
    case off
    case adsTrackers
    case adsTrackersMalware
}

/// Everything the user can change. Decoding tolerates missing keys so that
/// adding a setting in an update never wipes the others.
public struct HarborSettings: Codable, Equatable, Sendable {
    public var autoConnect: AutoConnect = .untrustedWiFi
    public var trustedNetworks: [String] = []
    /// Route everything through the tunnel (`includeAllNetworks`). Off by
    /// default: on iOS it can strand the phone offline during app updates.
    public var killSwitch = false
    /// Keep AirDrop, AirPlay, printers and CarPlay reachable.
    public var allowLAN = true
    public var threatProtection: ThreatProtection = .adsTrackers
    /// nil means Smart Location.
    public var selectedLocationID: String?
    public var favorites: [String] = []
    public var recents: [String] = []
    /// Days between automatic WireGuard key rotations.
    public var keyRotationDays = 7
    public var liveActivity = true

    public static let maxRecents = 5

    public init() {}

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = HarborSettings()
        autoConnect = (try? c.decodeIfPresent(AutoConnect.self, forKey: .autoConnect)) ?? d.autoConnect
        trustedNetworks = (try? c.decodeIfPresent([String].self, forKey: .trustedNetworks)) ?? d.trustedNetworks
        killSwitch = (try? c.decodeIfPresent(Bool.self, forKey: .killSwitch)) ?? d.killSwitch
        allowLAN = (try? c.decodeIfPresent(Bool.self, forKey: .allowLAN)) ?? d.allowLAN
        threatProtection = (try? c.decodeIfPresent(ThreatProtection.self, forKey: .threatProtection)) ?? d.threatProtection
        selectedLocationID = try? c.decodeIfPresent(String.self, forKey: .selectedLocationID)
        favorites = (try? c.decodeIfPresent([String].self, forKey: .favorites)) ?? d.favorites
        recents = (try? c.decodeIfPresent([String].self, forKey: .recents)) ?? d.recents
        keyRotationDays = (try? c.decodeIfPresent(Int.self, forKey: .keyRotationDays)) ?? d.keyRotationDays
        liveActivity = (try? c.decodeIfPresent(Bool.self, forKey: .liveActivity)) ?? d.liveActivity
    }

    public mutating func noteUsed(locationID: String) {
        recents.removeAll { $0 == locationID }
        recents.insert(locationID, at: 0)
        if recents.count > Self.maxRecents { recents.removeLast(recents.count - Self.maxRecents) }
    }

    public mutating func toggleFavorite(_ locationID: String) {
        if let i = favorites.firstIndex(of: locationID) {
            favorites.remove(at: i)
        } else {
            favorites.append(locationID)
        }
    }

    public mutating func trust(_ ssid: String) {
        let s = ssid.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty, !trustedNetworks.contains(s) else { return }
        trustedNetworks.append(s)
    }
}

/// Persists settings and shared state in the App Group so the app, tunnel
/// and widgets all see the same values.
public final class SharedStore: @unchecked Sendable {
    private let defaults: UserDefaults
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    public init(defaults: UserDefaults) { self.defaults = defaults }

    public convenience init?(appGroup: String) {
        guard let d = UserDefaults(suiteName: appGroup) else { return nil }
        self.init(defaults: d)
    }

    enum Key: String {
        case settings, serverList, tunnelSnapshot, accountTier, latencies
    }

    public var settings: HarborSettings {
        get { read(.settings) ?? HarborSettings() }
        set { write(.settings, newValue) }
    }

    public var serverList: ServerList? {
        get { read(.serverList) }
        set { write(.serverList, newValue) }
    }

    public var snapshot: TunnelSnapshot {
        get { read(.tunnelSnapshot) ?? TunnelSnapshot() }
        set { write(.tunnelSnapshot, newValue) }
    }

    public var tier: Tier {
        get { read(.accountTier) ?? .free }
        set { write(.accountTier, newValue) }
    }

    public var latencies: [String: Double] {
        get { read(.latencies) ?? [:] }
        set { write(.latencies, newValue) }
    }

    private func read<T: Decodable>(_ key: Key) -> T? {
        guard let data = defaults.data(forKey: key.rawValue) else { return nil }
        return try? decoder.decode(T.self, from: data)
    }

    private func write<T: Encodable>(_ key: Key, _ value: T?) {
        if let value, let data = try? encoder.encode(value) {
            defaults.set(data, forKey: key.rawValue)
        } else {
            defaults.removeObject(forKey: key.rawValue)
        }
    }
}

/// What widgets and the Live Activity show. Written by the app whenever the
/// tunnel status changes.
public struct TunnelSnapshot: Codable, Equatable, Sendable {
    public var connected = false
    public var connectedSince: Date?
    public var locationName: String?
    public var countryCode: String?
    public var smart = true

    public init(connected: Bool = false, connectedSince: Date? = nil, locationName: String? = nil,
                countryCode: String? = nil, smart: Bool = true) {
        self.connected = connected
        self.connectedSince = connectedSince
        self.locationName = locationName
        self.countryCode = countryCode
        self.smart = smart
    }
}
