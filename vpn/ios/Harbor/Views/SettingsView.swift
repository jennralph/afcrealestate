import HarborCore
import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Threat Protection", selection: model.binding(\.threatProtection)) {
                        Text("Off").tag(ThreatProtection.off)
                        Text("Ads & trackers").tag(ThreatProtection.adsTrackers)
                        Text("Ads, trackers & malware").tag(ThreatProtection.adsTrackersMalware)
                    }
                } header: {
                    Text("Protection")
                } footer: {
                    Text("Blocks ads, trackers and known malicious sites for every app, using Harbor's own DNS servers inside the tunnel. Nothing about your lookups is logged.")
                }

                Section {
                    Toggle("Block traffic outside VPN", isOn: model.binding(\.killSwitch))
                    Toggle("Allow local network", isOn: model.binding(\.allowLAN))
                } footer: {
                    Text("“Block traffic outside VPN” sends everything through Harbor, even while reconnecting. Disconnect before updating Harbor from the App Store, or the update can't download. Some Apple system services may still bypass any VPN. “Allow local network” keeps AirDrop, AirPlay, printers and CarPlay working.")
                }

                Section {
                    Picker("Auto-connect", selection: model.binding(\.autoConnect)) {
                        Text("Off").tag(AutoConnect.off)
                        Text("On untrusted Wi-Fi").tag(AutoConnect.untrustedWiFi)
                        Text("Always").tag(AutoConnect.always)
                    }
                    if model.settings.autoConnect != .off {
                        NavigationLink {
                            TrustedNetworksView()
                        } label: {
                            LabeledContent("Trusted networks", value: "\(model.settings.trustedNetworks.count)")
                        }
                    }
                } header: {
                    Text("Auto-connect")
                } footer: {
                    Text(autoConnectFooter)
                }

                Section("Account") {
                    NavigationLink {
                        AccountView()
                    } label: {
                        LabeledContent("Plan", value: model.tier == .paid ? "Harbor Plus" : "Free")
                    }
                }

                Section {
                    Picker("Rotate WireGuard key", selection: model.binding(\.keyRotationDays)) {
                        Text("Daily").tag(1)
                        Text("Weekly").tag(7)
                        Text("Monthly").tag(30)
                    }
                    Toggle("Live Activity while connected", isOn: model.binding(\.liveActivity))
                    NavigationLink("How Harbor protects your privacy") { PrivacyView() }
                } header: {
                    Text("Privacy")
                }

                Section("Help") {
                    NavigationLink("Diagnostics") { DiagnosticsView() }
                    Link("Support", destination: AppConfig.supportURL)
                    Link("Privacy Policy", destination: AppConfig.privacyPolicyURL)
                    Link("Terms of Use", destination: AppConfig.termsURL)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }

    var autoConnectFooter: String {
        switch model.settings.autoConnect {
        case .off: "Harbor connects only when you tap the button."
        case .untrustedWiFi: "Harbor connects by itself on public and unknown Wi-Fi, like cafés, hotels and airports, and leaves your trusted networks alone."
        case .always: "Harbor stays connected everywhere except your trusted Wi-Fi networks, and reconnects automatically if the connection drops."
        }
    }
}

struct TrustedNetworksView: View {
    @Environment(AppModel.self) private var model
    @State private var newName = ""

    var body: some View {
        List {
            Section {
                Button {
                    Task { await model.trustCurrentNetwork() }
                } label: {
                    Label("Trust current Wi-Fi", systemImage: "wifi")
                }
                HStack {
                    TextField("Network name", text: $newName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit(add)
                    Button("Add", action: add).disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            } footer: {
                Text("Harbor won't auto-connect on these networks. Only trust networks you control, like home.")
            }
            Section {
                ForEach(model.settings.trustedNetworks, id: \.self) { Text($0) }
                    .onDelete { offsets in model.update { $0.trustedNetworks.remove(atOffsets: offsets) } }
            }
        }
        .navigationTitle("Trusted Networks")
        .overlay {
            if model.settings.trustedNetworks.isEmpty {
                ContentUnavailableView("No trusted networks", systemImage: "wifi.exclamationmark",
                                       description: Text("Every Wi-Fi network is treated as untrusted."))
                    .padding(.top, 160)
            }
        }
    }

    func add() {
        let name = newName
        model.update { $0.trust(name) }
        newName = ""
    }
}

struct PrivacyView: View {
    var body: some View {
        List {
            item("number.square", "No email, no password",
                 "Your account is a random 16-digit number. We don't know who you are, and there's nothing to leak or sell.")
            item("doc.badge.ellipsis", "No activity or connection logs",
                 "We don't record the sites you visit, your DNS lookups, your real IP address, or when you connect. Our servers keep logs in memory only, and drop them within an hour.")
            item("eraser", "Your IP is wiped from memory",
                 "A few minutes after you go idle, each server forgets the address you connected from, even in its own memory.")
            item("key", "Keys made on your iPhone",
                 "Your WireGuard private key is generated on this device and never leaves it. Harbor rotates it automatically.")
            item("lock.shield", "Modern encryption",
                 "WireGuard uses ChaCha20-Poly1305 and Curve25519, the same protocol trusted by the Linux kernel.")
            item("creditcard", "Payments stay with Apple",
                 "Apple handles billing. We only receive a signed receipt saying your subscription is active, with no name or Apple ID.")
        }
        .navigationTitle("Privacy")
    }

    func item(_ icon: String, _ title: String, _ text: String) -> some View {
        Label {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(text).font(.subheadline).foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
        } icon: {
            Image(systemName: icon).foregroundStyle(Color.harborAccent)
        }
    }
}

struct DiagnosticsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        List {
            Section("Status") {
                LabeledContent("Connection", value: "\(model.state)".capitalized)
                if let spec = model.activeSpec {
                    LabeledContent("Server", value: spec.serverID)
                    LabeledContent("Location", value: spec.locationName)
                }
                if let hs = model.stats?.lastHandshake {
                    LabeledContent("Last handshake") { Text(hs, style: .relative) }
                }
                LabeledContent("Version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?")
            }
            if let spec = model.activeSpec {
                Section {
                    Text(spec.redactedConfig()).font(.caption.monospaced()).textSelection(.enabled)
                } header: {
                    Text("Configuration")
                } footer: {
                    Text("Safe to share with support. Your private key is never shown.")
                }
            }
        }
        .navigationTitle("Diagnostics")
    }
}
