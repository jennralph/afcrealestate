import Foundation
import HarborCore
import NetworkExtension
import os
import WireGuardKit

/// Runs WireGuard inside the system VPN. Kept deliberately small: iOS caps a
/// packet tunnel's memory at roughly 50 MB, and every feature the app can do
/// instead (server choice, key rotation, accounts) lives in the app.
final class PacketTunnelProvider: NEPacketTunnelProvider {
    private let log = Logger(subsystem: "net.harborvpn.app.tunnel", category: "tunnel")

    private lazy var adapter = WireGuardAdapter(with: self) { [log] level, message in
        switch level {
        case .error: log.error("\(message, privacy: .public)")
        case .verbose: log.debug("\(message, privacy: .public)")
        }
    }

    override func startTunnel(options: [String: NSObject]?, completionHandler: @escaping (Error?) -> Void) {
        let config: TunnelConfiguration
        do {
            config = try makeConfiguration()
        } catch {
            log.error("bad configuration: \(error.localizedDescription, privacy: .public)")
            completionHandler(error)
            return
        }
        adapter.start(tunnelConfiguration: config) { [log] adapterError in
            if let adapterError {
                log.error("start failed: \(String(describing: adapterError), privacy: .public)")
                completionHandler(TunnelError.startFailed)
            } else {
                completionHandler(nil)
            }
        }
    }

    override func stopTunnel(with reason: NEProviderStopReason, completionHandler: @escaping () -> Void) {
        adapter.stop { _ in completionHandler() }
    }

    /// The app polls this for the traffic counters on the home screen.
    override func handleAppMessage(_ messageData: Data, completionHandler: ((Data?) -> Void)?) {
        guard String(data: messageData, encoding: .utf8) == TunnelMessage.stats else {
            completionHandler?(nil)
            return
        }
        adapter.getRuntimeConfiguration { uapi in
            let stats = uapi.map(TunnelStats.parse(uapi:)) ?? TunnelStats()
            completionHandler?(try? JSONEncoder().encode(stats))
        }
    }

    private func makeConfiguration() throws -> TunnelConfiguration {
        guard let proto = protocolConfiguration as? NETunnelProviderProtocol,
              let spec = TunnelSpec(providerConfiguration: proto.providerConfiguration) else {
            throw TunnelError.missingConfiguration
        }
        guard let ref = proto.passwordReference,
              let keyData = SharedKeychain.data(forPersistentReference: ref),
              let privateKey = PrivateKey(rawValue: keyData) else {
            throw TunnelError.missingKey
        }
        guard let serverKey = PublicKey(base64Key: spec.serverPublicKey),
              let endpoint = Endpoint(from: spec.endpoint) else {
            throw TunnelError.missingConfiguration
        }

        var interface = InterfaceConfiguration(privateKey: privateKey)
        interface.addresses = spec.addresses.compactMap(IPAddressRange.init(from:))
        interface.dns = spec.dns.compactMap(DNSServer.init(from:))
        interface.mtu = UInt16(clamping: spec.mtu)

        var peer = PeerConfiguration(publicKey: serverKey)
        peer.endpoint = endpoint
        peer.allowedIPs = ["0.0.0.0/0", "::/0"].compactMap(IPAddressRange.init(from:))
        // Keeps NAT mappings alive on mobile networks so the tunnel doesn't
        // silently die while the phone is idle.
        peer.persistentKeepAlive = 25

        return TunnelConfiguration(name: spec.locationName, interface: interface, peers: [peer])
    }
}

enum TunnelError: LocalizedError {
    case missingConfiguration, missingKey, startFailed

    var errorDescription: String? {
        switch self {
        case .missingConfiguration: "The VPN profile is incomplete. Open Harbor to repair it."
        case .missingKey: "This device's key is missing. Open Harbor to sign in again."
        case .startFailed: "WireGuard could not start."
        }
    }
}
