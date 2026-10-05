import Foundation
import HarborCore
import NetworkExtension
import Observation
import SwiftUI
import UIKit
import WidgetKit

struct AlertItem: Identifiable {
    let id = UUID()
    var title: String
    var message: String
}

enum ConnectionState: Equatable {
    case disconnected, connecting, connected, reconnecting, disconnecting

    init(_ status: NEVPNStatus) {
        switch status {
        case .connected: self = .connected
        case .connecting: self = .connecting
        case .reasserting: self = .reconnecting
        case .disconnecting: self = .disconnecting
        default: self = .disconnected
        }
    }

    var isActive: Bool { self == .connected || self == .connecting || self == .reconnecting }
}

/// The app's single source of truth.
@MainActor
@Observable
final class AppModel {
    enum Phase { case launching, onboarding, ready }

    var phase: Phase = .launching
    private(set) var accountNumber: String?
    /// Shown once, right after creating an account.
    var newAccountNumber: String?
    private(set) var account: AccountInfo?
    private(set) var serverList: ServerList?
    private(set) var settings: HarborSettings
    private(set) var state: ConnectionState = .disconnected
    private(set) var connectedSince: Date?
    private(set) var stats: TunnelStats?
    private(set) var latencies: [String: Double]
    private(set) var activeSpec: TunnelSpec?
    var alert: AlertItem?
    var showPaywall = false
    var busy = false

    @ObservationIgnored let api = APIClient(baseURL: AppConfig.apiURL)
    @ObservationIgnored let tunnel = TunnelController()
    @ObservationIgnored let purchases = PurchaseManager()
    @ObservationIgnored private let activities = LiveActivityManager()
    @ObservationIgnored private let store: SharedStore
    @ObservationIgnored private var statsTask: Task<Void, Never>?

    init() {
        store = SharedStore(appGroup: AppConfig.appGroup) ?? SharedStore(defaults: .standard)
        settings = store.settings
        latencies = store.latencies
        serverList = store.serverList
    }

    // MARK: Derived

    var tier: Tier { account?.tier ?? store.tier }
    var countries: [Country] { serverList?.countries() ?? [] }

    var thisDevice: DeviceInfo? {
        guard let key = DeviceKey.current?.publicKeyBase64 else { return nil }
        return account?.devices.first { $0.publicKey == key }
    }

    var selectedLocation: Location? {
        settings.selectedLocationID.flatMap { serverList?.location(id: $0) }
    }

    func location(id: String) -> Location? { serverList?.location(id: id) }

    func canUse(_ location: Location) -> Bool { tier == .paid || location.isFree }

    /// Lowest measured latency in a location, for the list badges.
    func latency(of location: Location) -> Double? {
        location.servers.compactMap { latencies[$0.id] }.min()
    }

    // MARK: Lifecycle

    func bootstrap() async {
        purchases.deliver = { [weak self] jws in await self?.submitPurchase(jws) ?? false }
        purchases.start()
        tunnel.onStatusChange = { [weak self] status, date in self?.tunnelStatusChanged(status, date) }
        await tunnel.load()
        activeSpec = tunnel.currentSpec

        if let data = SharedKeychain.data(for: .accountNumber), let number = String(data: data, encoding: .utf8) {
            accountNumber = number
            phase = .ready
            await refresh()
        } else {
            phase = .onboarding
            await refreshServers()
        }
    }

    /// Called on launch and whenever the app comes to the foreground.
    func refresh() async {
        guard accountNumber != nil else { return }
        async let servers: Void = refreshServers()
        async let acct: Void = refreshAccount()
        _ = await (servers, acct)
        if !state.isActive { await probeLatencies() }
    }

    func refreshServers() async {
        do {
            let list = try await api.servers()
            serverList = list
            store.serverList = list
        } catch {
            // Keep the cached list; connecting still works with it.
        }
    }

    func refreshAccount() async {
        guard let number = accountNumber else { return }
        do {
            let info = try await api.account(number: number)
            account = info
            store.tier = info.tier
        } catch let e as APIError where e.status == 401 {
            // Account no longer exists (e.g. deleted). Start over.
            await signOut(removeDevice: false)
            alert = AlertItem(title: "Signed out", message: e.message)
        } catch {}
    }

    func probeLatencies() async {
        guard let servers = serverList?.servers, !servers.isEmpty else { return }
        let fresh = await LatencyProbe.measure(ServerSelection.eligible(servers, tier: .paid))
        guard !fresh.isEmpty else { return }
        latencies = fresh
        store.latencies = fresh
    }

    // MARK: Account

    func createAccount() async {
        busy = true
        defer { busy = false }
        do {
            let info = try await api.createAccount()
            guard let number = info.number else { throw APIError(code: "internal", message: "No account number returned.") }
            SharedKeychain.set(Data(number.utf8), for: .accountNumber)
            accountNumber = number
            account = info
            newAccountNumber = number
            phase = .ready
            _ = try? await ensureDevice()
            await probeLatencies()
        } catch {
            show(error, title: "Couldn't create an account")
        }
    }

    func signIn(_ input: String) async -> Bool {
        guard let number = AccountNumber.normalize(input) else {
            alert = AlertItem(title: "Check the number", message: "Account numbers have 16 digits.")
            return false
        }
        busy = true
        defer { busy = false }
        do {
            account = try await api.account(number: number)
            SharedKeychain.set(Data(number.utf8), for: .accountNumber)
            accountNumber = number
            store.tier = account?.tier ?? .free
            phase = .ready
            _ = try? await ensureDevice()
            await purchases.syncEntitlements()
            await probeLatencies()
            return true
        } catch {
            show(error, title: "Couldn't sign in")
            return false
        }
    }

    func signOut(removeDevice: Bool = true) async {
        await disconnect()
        if removeDevice, let number = accountNumber, let device = thisDevice {
            try? await api.removeDevice(number: number, deviceID: device.id)
        }
        await tunnel.remove()
        SharedKeychain.delete(.accountNumber)
        SharedKeychain.delete(.wireGuardKey)
        accountNumber = nil
        account = nil
        store.tier = .free
        activeSpec = nil
        phase = .onboarding
    }

    func removeDevice(_ device: DeviceInfo) async {
        guard let number = accountNumber else { return }
        do {
            try await api.removeDevice(number: number, deviceID: device.id)
            await refreshAccount()
        } catch {
            show(error, title: "Couldn't remove device")
        }
    }

    /// Makes sure this iPhone has a key registered on the account.
    @discardableResult
    func ensureDevice() async throws -> DeviceInfo {
        guard let number = accountNumber else { throw APIError(code: "no_account", message: "Sign in first.") }
        if let d = thisDevice { return d }
        var pair = DeviceKey.current ?? DeviceKey.generate()
        let device: DeviceInfo
        do {
            device = try await api.registerDevice(number: number, publicKey: pair.publicKeyBase64, name: UIDevice.current.name)
        } catch let e as APIError where e.code == "key_in_use" {
            // The stored key belongs to another account (signed in before).
            pair = DeviceKey.generate()
            device = try await api.registerDevice(number: number, publicKey: pair.publicKeyBase64, name: UIDevice.current.name)
        }
        guard DeviceKey.save(pair) != nil else {
            throw APIError(code: "keychain", message: "Couldn't save this device's key.")
        }
        await refreshAccount()
        return account?.devices.first { $0.id == device.id } ?? device
    }

    /// Replaces the WireGuard key on schedule so a key that ever leaked stops
    /// working. Only done while disconnected; the tunnel holds the old key.
    func rotateKeyIfDue() async {
        guard !state.isActive, let number = accountNumber, let device = thisDevice,
              KeyRotation.isDue(last: device.keyRotated, now: .now, everyDays: settings.keyRotationDays) else { return }
        let pair = DeviceKey.generate()
        do {
            _ = try await api.rotateKey(number: number, deviceID: device.id, publicKey: pair.publicKeyBase64)
            _ = DeviceKey.save(pair)
            await refreshAccount()
        } catch {
            // Not fatal: the current key keeps working; try again next time.
        }
    }

    // MARK: Connecting

    func toggle() async {
        if state.isActive { await disconnect() } else { await connect() }
    }

    /// Choose a location (nil = Smart Location) and connect to it.
    func select(_ location: Location?) async {
        if let location, !canUse(location) {
            showPaywall = true
            return
        }
        settings.selectedLocationID = location?.id
        store.settings = settings
        if state.isActive { await tunnel.stop() }
        await connect()
    }

    func connect() async {
        guard accountNumber != nil else { return }
        busy = true
        defer { busy = false }
        do {
            if serverList == nil { await refreshServers() }
            guard let list = serverList else {
                throw APIError(code: "offline", message: "Couldn't load locations. Check your internet connection.")
            }
            await rotateKeyIfDue()
            let device = try await ensureDevice()
            let server = try pickServer(from: list)
            guard let keyRef = DeviceKey.reference else {
                throw APIError(code: "keychain", message: "This device's key is missing. Sign out and back in.")
            }
            let spec = TunnelSpec(server: server, device: device, dns: list.dns.servers(for: settings.threatProtection))
            try await tunnel.install(spec: spec, keyReference: keyRef, settings: settings)
            activeSpec = spec
            try tunnel.start()
            settings.noteUsed(locationID: server.locationID)
            store.settings = settings
        } catch where error is URLError && tunnel.isInstalled {
            // Harbor's API is unreachable (captive portal, outage). The
            // installed profile still works, so connect with it rather than
            // leave the user unprotected.
            try? tunnel.start()
        } catch let e as NEVPNError where e.code == .configurationReadWriteFailed {
            alert = AlertItem(title: "VPN permission needed",
                              message: "Tap Allow when iOS asks to add Harbor's VPN configuration.")
        } catch {
            show(error, title: "Couldn't connect")
        }
    }

    func disconnect() async {
        await tunnel.stop()
    }

    private func pickServer(from list: ServerList) throws -> Server {
        var context = ServerSelection.Context.current
        context.latencies = latencies
        if let location = selectedLocation {
            guard canUse(location) else {
                showPaywall = true
                throw APIError(code: "upgrade", message: "\(location.city) is available with Harbor Plus.")
            }
            if let s = ServerSelection.best(in: location, tier: tier, context: context) { return s }
        }
        guard let s = ServerSelection.smart(list.servers, tier: tier, context: context) else {
            throw APIError(code: "no_servers", message: "No servers are available right now. Please try again shortly.")
        }
        return s
    }

    // MARK: Settings

    /// Applies a settings change and, where needed, rebuilds the VPN profile.
    func update(_ change: (inout HarborSettings) -> Void) {
        let old = settings
        change(&settings)
        guard settings != old else { return }
        store.settings = settings
        let new = settings
        Task { await apply(from: old, to: new) }
    }

    func binding<T>(_ keyPath: WritableKeyPath<HarborSettings, T>) -> Binding<T> {
        Binding(get: { self.settings[keyPath: keyPath] },
                set: { value in self.update { $0[keyPath: keyPath] = value } })
    }

    private func apply(from old: HarborSettings, to new: HarborSettings) async {
        guard tunnel.isInstalled else { return }
        let needsRebuild = old.killSwitch != new.killSwitch || old.allowLAN != new.allowLAN
            || old.threatProtection != new.threatProtection
        if needsRebuild {
            if state.isActive {
                await tunnel.stop()
                await connect()
            } else if let spec = activeSpec, let ref = DeviceKey.reference, let list = serverList {
                var updated = spec
                updated.dns = list.dns.servers(for: new.threatProtection)
                try? await tunnel.install(spec: updated, keyReference: ref, settings: new)
                activeSpec = updated
            }
        } else if old.autoConnect != new.autoConnect || old.trustedNetworks != new.trustedNetworks {
            try? await tunnel.updateOnDemand(new)
        }
    }

    func trustCurrentNetwork() async {
        guard let ssid = await NEHotspotNetwork.fetchCurrent()?.ssid else {
            alert = AlertItem(title: "Not on Wi-Fi", message: "Join the Wi-Fi network you want to trust, then try again.")
            return
        }
        update { $0.trust(ssid) }
    }

    // MARK: Purchases

    func submitPurchase(_ jws: String) async -> Bool {
        guard let number = accountNumber else { return false }
        do {
            account = try await api.submitPurchase(number: number, signedTransaction: jws)
            store.tier = account?.tier ?? .free
            if account?.tier == .paid { showPaywall = false }
            return true
        } catch {
            return false
        }
    }

    // MARK: Status

    private func tunnelStatusChanged(_ status: NEVPNStatus, _ date: Date?) {
        state = ConnectionState(status)
        connectedSince = state == .connected ? (date ?? .now) : nil
        activeSpec = tunnel.currentSpec ?? activeSpec

        let spec = activeSpec
        store.snapshot = TunnelSnapshot(connected: state == .connected, connectedSince: connectedSince,
                                        locationName: spec?.locationName, countryCode: spec?.countryCode,
                                        smart: settings.selectedLocationID == nil)
        WidgetCenter.shared.reloadAllTimelines()
        if #available(iOS 18.0, *) { ControlCenter.shared.reloadAllControls() }

        if settings.liveActivity, let spec, state == .connected || state == .reconnecting {
            activities.show(locationName: spec.locationName, countryCode: spec.countryCode,
                            since: connectedSince ?? .now, reconnecting: state == .reconnecting)
        } else if !state.isActive {
            activities.end()
        }

        statsTask?.cancel()
        stats = nil
        if state == .connected {
            statsTask = Task { [weak self] in
                while !Task.isCancelled {
                    guard let self else { return }
                    self.stats = await self.tunnel.stats()
                    try? await Task.sleep(for: .seconds(1))
                }
            }
        }
    }

    private func show(_ error: Error, title: String) {
        alert = AlertItem(title: title, message: error.localizedDescription)
    }
}
