import ActivityKit
import Foundation

/// The Lock Screen / Dynamic Island Live Activity shown while connected.
struct HarborActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var locationName: String
        var countryCode: String
        var connectedSince: Date
        var reconnecting: Bool
    }
}
