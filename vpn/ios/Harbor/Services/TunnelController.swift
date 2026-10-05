import Foundation
import HarborCore
import NetworkExtension

/// Owns the app's VPN profile: installs it with the chosen server and the
/// user's protection settings, starts and stops it, and reports status.
@MainActor
final class TunnelController {
    private(set) var manager: NETunnelProviderManager?
    var onStatusChange: ((NEVPNStatus, Date?) -> Void)?
    private var observer: NSObjectProtocol?

    var status: NEVPNStatus { manager?.connection.status ?? .disconnected }
    var connectedDate: Date? { manager?.connection.connectedDate }
    var isInstalled: Bool { manager != nil }
    var currentSpec: TunnelSpec? {
        TunnelSpec(providerConfiguration: (manager?.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration)
    }

    func load() async {
        manager = await VPNSwitch.manager()
        observer = NotificationCenter.default.addObserver(
            forName: .NEVPNStatusDidChange, object: nil, queue: .main
        ) { [weak self] note in
            MainActor.assumeIsolated {
                guard let self, let conn = note.object as? NEVPNConnection,
                      conn === self.manager?.connection else { return }
                self.onStatusChange?(conn.status, conn.connectedDate)
            }
        }
        onStatusChange?(status, connectedDate)
    }

    /// Writes the profile. The first call shows iOS's "Add VPN
    /// Configurations" prompt.
    func install(spec: TunnelSpec, keyReference: Data, settings: HarborSettings) async throws {
        let m = manager ?? NETunnelProviderManager()
        let proto = (m.protocolConfiguration as? NETunnelProviderProtocol) ?? NETunnelProviderProtocol()
        proto.providerBundleIdentifier = AppConfig.tunnelBundleID
        proto.serverAddress = spec.locationName // shown in Settings › VPN
        proto.providerConfiguration = try spec.providerConfiguration()
        proto.passwordReference = keyReference
        proto.disconnectOnSleep = false
        proto.includeAllNetworks = settings.killSwitch
        proto.excludeLocalNetworks = settings.allowLAN
        proto.enforceRoutes = settings.killSwitch
        m.protocolConfiguration = proto
        m.localizedDescription = "Harbor VPN"
        m.isEnabled = true
        applyOnDemand(settings, to: m)
        try await m.saveToPreferences()
        // NetworkExtension wants a reload after saving before it will start.
        try await m.loadFromPreferences()
        let isNew = manager == nil
        manager = m
        if isNew { await load() }
    }

    /// Updates only the auto-connect rules on an existing profile.
    func updateOnDemand(_ settings: HarborSettings) async throws {
        guard let m = manager else { return }
        applyOnDemand(settings, to: m)
        try await m.saveToPreferences()
    }

    func start() throws {
        try manager?.connection.startVPNTunnel()
    }

    func stop() async {
        try? await VPNSwitch.turnOff()
    }

    func remove() async {
        try? await manager?.removeFromPreferences()
        manager = nil
    }

    func stats() async -> TunnelStats? {
        guard let session = manager?.connection as? NETunnelProviderSession, status == .connected else { return nil }
        return await withCheckedContinuation { cont in
            do {
                try session.sendProviderMessage(Data(TunnelMessage.stats.utf8)) { data in
                    cont.resume(returning: data.flatMap { try? JSONDecoder().decode(TunnelStats.self, from: $0) })
                }
            } catch {
                cont.resume(returning: nil)
            }
        }
    }

    private func applyOnDemand(_ settings: HarborSettings, to m: NETunnelProviderManager) {
        guard let specs = OnDemandPolicy.rules(for: settings) else {
            m.onDemandRules = nil
            m.isOnDemandEnabled = false
            return
        }
        m.onDemandRules = specs.map { spec in
            let rule: NEOnDemandRule = switch spec.action {
            case .connect: NEOnDemandRuleConnect()
            case .disconnect: NEOnDemandRuleDisconnect()
            case .ignore: NEOnDemandRuleIgnore()
            }
            rule.interfaceTypeMatch = switch spec.interface {
            case .any: .any
            case .wifi: .wiFi
            case .cellular: .cellular
            }
            if !spec.ssids.isEmpty { rule.ssidMatch = spec.ssids }
            return rule
        }
        m.isOnDemandEnabled = true
    }
}
