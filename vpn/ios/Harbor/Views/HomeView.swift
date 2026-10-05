import HarborCore
import SwiftUI

struct HomeView: View {
    @Environment(AppModel.self) private var model
    @State private var showLocations = false
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 28) {
                    StatusHeader()
                    ConnectButton()
                        .padding(.vertical, 8)
                    LocationCard { showLocations = true }
                    if model.state == .connected { TrafficStats() }
                    RecentsRow()
                    if model.tier == .free { UpgradeBanner() }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 32)
            }
            .background(Background(protected: model.state == .connected))
            .navigationTitle("Harbor")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
            }
            .sheet(isPresented: $showLocations) { LocationsView() }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .refreshable { await model.refresh() }
        }
    }
}

private struct Background: View {
    let protected: Bool
    var body: some View {
        LinearGradient(colors: [(protected ? Color.harborProtected : Color.harborAccent).opacity(0.18), .clear],
                       startPoint: .top, endPoint: .center)
            .ignoresSafeArea()
            .animation(.easeInOut(duration: 0.6), value: protected)
    }
}

private struct StatusHeader: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 6) {
            Label(title, systemImage: icon)
                .font(.title2.weight(.semibold))
                .foregroundStyle(color)
                .contentTransition(.symbolEffect(.replace))
            Group {
                if model.state == .connected, let since = model.connectedSince {
                    Text(since, style: .timer).monospacedDigit()
                } else {
                    Text(subtitle)
                }
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        .padding(.top, 12)
        .accessibilityElement(children: .combine)
    }

    var title: String {
        switch model.state {
        case .connected: "Protected"
        case .connecting: "Connecting…"
        case .reconnecting: "Reconnecting…"
        case .disconnecting: "Disconnecting…"
        case .disconnected: "Not protected"
        }
    }

    var subtitle: String {
        switch model.state {
        case .disconnected: "Your IP address and activity are visible"
        case .reconnecting: "Your traffic is held until the tunnel is back"
        default: " "
        }
    }

    var icon: String { model.state == .connected ? "lock.shield.fill" : "shield.slash" }
    var color: Color {
        switch model.state {
        case .connected: .harborProtected
        case .disconnected: .primary
        default: .orange
        }
    }
}

struct ConnectButton: View {
    @Environment(AppModel.self) private var model
    @State private var spin = false

    var body: some View {
        Button {
            Task { await model.toggle() }
        } label: {
            ZStack {
                Circle()
                    .fill(fill.gradient)
                    .shadow(color: fill.opacity(0.45), radius: 24, y: 10)
                Circle()
                    .trim(from: 0, to: busy ? 0.28 : 0)
                    .stroke(.white.opacity(0.85), style: StrokeStyle(lineWidth: 5, lineCap: .round))
                    .padding(10)
                    .rotationEffect(.degrees(spin ? 360 : 0))
                Image(systemName: "power")
                    .font(.system(size: 64, weight: .semibold))
                    .foregroundStyle(.white)
            }
            .frame(width: 190, height: 190)
        }
        .buttonStyle(PressableStyle())
        .sensoryFeedback(.impact(weight: .medium), trigger: model.state == .connected)
        .disabled(model.busy && !model.state.isActive)
        .accessibilityLabel(model.state.isActive ? "Disconnect" : "Connect")
        .onChange(of: busy, initial: true) { _, isBusy in
            spin = false
            if isBusy {
                withAnimation(.linear(duration: 1).repeatForever(autoreverses: false)) { spin = true }
            }
        }
    }

    var busy: Bool { model.state == .connecting || model.state == .reconnecting || model.state == .disconnecting || model.busy }
    var fill: Color {
        switch model.state {
        case .connected: .harborProtected
        case .disconnected: .harborAccent
        default: .orange
        }
    }
}

private struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .animation(.spring(duration: 0.25), value: configuration.isPressed)
    }
}

private struct LocationCard: View {
    @Environment(AppModel.self) private var model
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Text(flag).font(.system(size: 34))
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.selectedLocation == nil ? "Smart Location" : "Location")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(name).font(.headline).foregroundStyle(.primary)
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            .padding(16)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    var name: String {
        if let loc = model.selectedLocation { return loc.name }
        if model.state.isActive, let spec = model.activeSpec { return spec.locationName }
        return "Fastest available"
    }

    var flag: String {
        if let loc = model.selectedLocation { return loc.flag }
        if model.state.isActive, let cc = model.activeSpec?.countryCode { return Flag.emoji(cc) }
        return "⚡️"
    }
}

private struct TrafficStats: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 12) {
            tile("Download", systemImage: "arrow.down", value: model.stats.map { Format.bytes($0.rxBytes) } ?? "—")
            tile("Upload", systemImage: "arrow.up", value: model.stats.map { Format.bytes($0.txBytes) } ?? "—")
            tile("Protection", systemImage: "hand.raised.fill", value: protection)
        }
    }

    var protection: String {
        switch model.settings.threatProtection {
        case .off: "DNS only"
        case .adsTrackers: "Ads blocked"
        case .adsTrackersMalware: "Max"
        }
    }

    func tile(_ title: String, systemImage: String, value: String) -> some View {
        VStack(spacing: 4) {
            Image(systemName: systemImage).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.callout.weight(.semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

private struct RecentsRow: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let recents = model.settings.recents.compactMap(model.location(id:))
            .filter { $0.id != model.settings.selectedLocationID }
        if !recents.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Recent").font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 10) {
                        ForEach(recents) { loc in
                            Button {
                                Task { await model.select(loc) }
                            } label: {
                                Text("\(loc.flag) \(loc.city)")
                                    .font(.subheadline)
                                    .padding(.horizontal, 14).padding(.vertical, 9)
                                    .background(.thinMaterial, in: Capsule())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct UpgradeBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Button { model.showPaywall = true } label: {
            HStack(spacing: 12) {
                Image(systemName: "sparkles").font(.title3).foregroundStyle(Color.harborAccent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("You're on Harbor Free").font(.subheadline.weight(.semibold))
                    Text("Unlimited data, no ads. Upgrade for every location and up to 7 devices.")
                        .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .padding(16)
            .background(Color.harborAccent.opacity(0.1), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
        .buttonStyle(.plain)
    }
}
