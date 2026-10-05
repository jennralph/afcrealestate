import Foundation

/// A platform-neutral description of one NEOnDemandRule, so the policy can be
/// unit-tested without NetworkExtension.
public struct OnDemandRuleSpec: Equatable, Sendable {
    public enum Action: Equatable, Sendable { case connect, disconnect, ignore }
    public enum Interface: Equatable, Sendable { case any, wifi, cellular }

    public var action: Action
    public var interface: Interface
    public var ssids: [String]

    public init(_ action: Action, _ interface: Interface, ssids: [String] = []) {
        self.action = action
        self.interface = interface
        self.ssids = ssids
    }
}

public enum OnDemandPolicy {
    /// Rules for the settings, evaluated first-match by iOS, or nil when
    /// on-demand should be off.
    public static func rules(for settings: HarborSettings) -> [OnDemandRuleSpec]? {
        let trusted = settings.trustedNetworks
        var rules: [OnDemandRuleSpec] = []
        switch settings.autoConnect {
        case .off:
            return nil
        case .untrustedWiFi:
            if !trusted.isEmpty { rules.append(.init(.disconnect, .wifi, ssids: trusted)) }
            rules.append(.init(.connect, .wifi))
            // On cellular, leave the tunnel as the user left it.
            rules.append(.init(.ignore, .any))
        case .always:
            if !trusted.isEmpty { rules.append(.init(.disconnect, .wifi, ssids: trusted)) }
            rules.append(.init(.connect, .any))
        }
        return rules
    }
}

public enum KeyRotation {
    /// Whether a key last rotated at `last` should be replaced at `now`.
    public static func isDue(last: Date, now: Date, everyDays days: Int) -> Bool {
        guard days > 0 else { return false }
        return now.timeIntervalSince(last) >= Double(days) * 86_400
    }
}
