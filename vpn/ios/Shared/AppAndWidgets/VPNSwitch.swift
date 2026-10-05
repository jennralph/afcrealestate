import Foundation
import NetworkExtension

/// Minimal on/off control over the installed VPN profile, usable from the
/// widget extension (Control Center toggle, home screen widget) as well as
/// the app. Choosing a server and installing the profile is the app's job.
enum VPNSwitch {
    static func manager() async -> NETunnelProviderManager? {
        let all = try? await NETunnelProviderManager.loadAllFromPreferences()
        return all?.first { ($0.protocolConfiguration as? NETunnelProviderProtocol)?.providerBundleIdentifier == AppConfig.tunnelBundleID }
    }

    static func isOn() async -> Bool {
        guard let m = await manager() else { return false }
        switch m.connection.status {
        case .connected, .connecting, .reasserting: return true
        default: return false
        }
    }

    enum SwitchError: LocalizedError {
        case notSetUp
        var errorDescription: String? { "Open Harbor once to finish setting up the VPN." }
    }

    static func turnOn() async throws {
        guard let m = await manager() else { throw SwitchError.notSetUp }
        if !m.isEnabled {
            m.isEnabled = true
            try await m.saveToPreferences()
            try await m.loadFromPreferences()
        }
        try m.connection.startVPNTunnel()
    }

    /// Stops the tunnel. With auto-connect on, iOS would bring it straight
    /// back, so on-demand is paused until the user connects again; the
    /// app turns it back on from the saved settings.
    static func turnOff() async throws {
        guard let m = await manager() else { return }
        if m.isOnDemandEnabled {
            m.isOnDemandEnabled = false
            try await m.saveToPreferences()
        }
        m.connection.stopVPNTunnel()
    }
}
