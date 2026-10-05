import ActivityKit
import AppIntents
import HarborCore
import SwiftUI
import WidgetKit

@main
struct HarborWidgetsBundle: WidgetBundle {
    var body: some Widget {
        StatusWidget()
        HarborLiveActivity()
        if #available(iOS 18.0, *) {
            VPNControl()
        }
    }
}

private let protectedGreen = Color(red: 0.13, green: 0.75, blue: 0.47)
private let accentBlue = Color(red: 0.16, green: 0.45, blue: 0.98)

// MARK: - Home & Lock Screen widget

struct StatusEntry: TimelineEntry {
    let date: Date
    let snapshot: TunnelSnapshot
}

struct StatusProvider: TimelineProvider {
    func placeholder(in context: Context) -> StatusEntry {
        StatusEntry(date: .now, snapshot: TunnelSnapshot(connected: true, connectedSince: .now, locationName: "Stockholm, Sweden", countryCode: "se"))
    }

    func getSnapshot(in context: Context, completion: @escaping (StatusEntry) -> Void) {
        completion(context.isPreview ? placeholder(in: context) : current())
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<StatusEntry>) -> Void) {
        // The app reloads timelines whenever the tunnel changes state.
        completion(Timeline(entries: [current()], policy: .never))
    }

    private func current() -> StatusEntry {
        let snapshot = SharedStore(appGroup: AppConfig.appGroup)?.snapshot ?? TunnelSnapshot()
        return StatusEntry(date: .now, snapshot: snapshot)
    }
}

struct StatusWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "HarborStatus", provider: StatusProvider()) { entry in
            StatusWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Harbor Status")
        .description("See if you're protected and connect with one tap.")
        .supportedFamilies([.systemSmall, .accessoryCircular, .accessoryRectangular, .accessoryInline])
    }
}

struct StatusWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: StatusEntry
    var s: TunnelSnapshot { entry.snapshot }

    var body: some View {
        switch family {
        case .accessoryCircular:
            Button(intent: SetVPNIntent(!s.connected)) {
                Image(systemName: s.connected ? "lock.shield.fill" : "shield.slash")
                    .font(.title2)
            }
            .buttonStyle(.plain)
            .widgetAccentable()
        case .accessoryInline:
            Label(s.connected ? "Protected" : "Not protected", systemImage: s.connected ? "lock.shield.fill" : "shield.slash")
        case .accessoryRectangular:
            VStack(alignment: .leading) {
                Label(s.connected ? "Protected" : "Not protected", systemImage: s.connected ? "lock.shield.fill" : "shield.slash")
                    .font(.headline)
                if s.connected, let name = s.locationName { Text(name).font(.caption) }
            }
        default:
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Image(systemName: s.connected ? "lock.shield.fill" : "shield.slash")
                        .font(.title2)
                        .foregroundStyle(s.connected ? protectedGreen : .secondary)
                    Spacer()
                    if s.connected, let cc = s.countryCode { Text(Flag.emoji(cc)).font(.title2) }
                }
                Spacer()
                Text(s.connected ? "Protected" : "Not protected").font(.headline)
                if s.connected, let since = s.connectedSince {
                    Text(since, style: .timer).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                } else {
                    Text(s.smart ? "Smart Location" : (s.locationName ?? "")).font(.caption).foregroundStyle(.secondary)
                }
                Button(intent: SetVPNIntent(!s.connected)) {
                    Text(s.connected ? "Disconnect" : "Connect")
                        .font(.caption.weight(.semibold))
                        .frame(maxWidth: .infinity)
                }
                .tint(s.connected ? .gray : accentBlue)
            }
        }
    }
}

// MARK: - Control Center toggle (iOS 18)

@available(iOS 18.0, *)
struct VPNControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "net.harborvpn.control", provider: VPNValueProvider()) { isOn in
            ControlWidgetToggle("Harbor VPN", isOn: isOn, action: SetVPNIntent()) { on in
                Label(on ? "Protected" : "Off", systemImage: on ? "lock.shield.fill" : "shield.slash")
            }
            .tint(protectedGreen)
        }
        .displayName("Harbor VPN")
        .description("Turn the VPN on or off from Control Center or the Lock Screen.")
    }
}

@available(iOS 18.0, *)
struct VPNValueProvider: ControlValueProvider {
    var previewValue: Bool { false }

    func currentValue() async throws -> Bool {
        await VPNSwitch.isOn()
    }
}

// MARK: - Live Activity

struct HarborLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: HarborActivityAttributes.self) { context in
            HStack(spacing: 14) {
                Image(systemName: "lock.shield.fill").font(.title).foregroundStyle(protectedGreen)
                VStack(alignment: .leading, spacing: 2) {
                    Text(context.state.reconnecting ? "Reconnecting…" : "Protected").font(.headline)
                    Text("\(Flag.emoji(context.state.countryCode)) \(context.state.locationName)")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Text(context.state.connectedSince, style: .timer)
                    .font(.title3.monospacedDigit())
                    .multilineTextAlignment(.trailing)
                    .frame(width: 90)
            }
            .padding()
            .activityBackgroundTint(Color.black.opacity(0.6))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label("Harbor", systemImage: "lock.shield.fill").foregroundStyle(protectedGreen)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.connectedSince, style: .timer).monospacedDigit().frame(width: 70)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text("\(Flag.emoji(context.state.countryCode)) \(context.state.locationName)")
                        Spacer()
                        Button(intent: SetVPNIntent(false)) { Text("Disconnect") }
                            .tint(.red)
                    }
                }
            } compactLeading: {
                Image(systemName: "lock.shield.fill").foregroundStyle(context.state.reconnecting ? .orange : protectedGreen)
            } compactTrailing: {
                Text(Flag.emoji(context.state.countryCode))
            } minimal: {
                Image(systemName: "lock.shield.fill").foregroundStyle(protectedGreen)
            }
        }
    }
}
