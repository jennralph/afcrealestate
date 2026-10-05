import Foundation

/// Picks servers. "Smart Location" is the default most people never change, so
/// it has to be good: measured latency when we have it, a rough geographic
/// guess when we don't, and always steering away from crowded servers.
public enum ServerSelection {
    /// Inputs that describe where the phone probably is.
    public struct Context: Sendable {
        /// Measured round-trip times in milliseconds, keyed by server ID.
        public var latencies: [String: Double]
        /// ISO region of the device locale, e.g. "SE".
        public var regionCode: String?
        /// Seconds east of UTC; used to estimate longitude when nothing is measured.
        public var utcOffsetSeconds: Int

        public init(latencies: [String: Double] = [:], regionCode: String? = nil, utcOffsetSeconds: Int = 0) {
            self.latencies = latencies
            self.regionCode = regionCode
            self.utcOffsetSeconds = utcOffsetSeconds
        }

        public static var current: Context {
            Context(regionCode: Locale.current.region?.identifier,
                    utcOffsetSeconds: TimeZone.current.secondsFromGMT())
        }
    }

    /// Servers an account may use.
    public static func eligible(_ servers: [Server], tier: Tier) -> [Server] {
        tier == .paid ? servers : servers.filter(\.free)
    }

    /// The best server overall.
    public static func smart(_ servers: [Server], tier: Tier, context: Context) -> Server? {
        best(eligible(servers, tier: tier), context: context)
    }

    /// The best server within one location.
    public static func best(in location: Location, tier: Tier, context: Context) -> Server? {
        best(eligible(location.servers, tier: tier), context: context)
    }

    static func best(_ candidates: [Server], context: Context) -> Server? {
        guard !candidates.isEmpty else { return nil }
        let measured = candidates.filter { context.latencies[$0.id] != nil }
        if !measured.isEmpty {
            return measured.min { score($0, context) < score($1, context) }
        }
        if let region = context.regionCode?.lowercased() {
            let local = candidates.filter { $0.countryCode.lowercased() == region }
            if let pick = local.min(by: { ($0.load, $0.id) < ($1.load, $1.id) }) { return pick }
        }
        // No measurements and no server in our country: the time zone gives a
        // decent longitude estimate (15° per hour), which at least keeps
        // someone in Tokyo off a server in Chicago.
        let lon = Double(context.utcOffsetSeconds) / 3600 * 15
        return candidates.min {
            let a = (longitudeDistance($0.longitude, lon), $0.load)
            let b = (longitudeDistance($1.longitude, lon), $1.load)
            return a < b
        }
    }

    /// Lower is better. Load costs nothing until 60% and then up to ~120 ms
    /// at 100%, so a nearby busy server loses to a slightly further idle one.
    static func score(_ s: Server, _ context: Context) -> Double {
        let latency = context.latencies[s.id] ?? 1000
        let crowding = max(0, Double(s.load) - 60) * 3
        return latency + crowding
    }

    static func longitudeDistance(_ a: Double, _ b: Double) -> Double {
        let d = abs(a - b).truncatingRemainder(dividingBy: 360)
        return min(d, 360 - d)
    }
}
