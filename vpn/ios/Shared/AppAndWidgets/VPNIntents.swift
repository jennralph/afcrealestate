import AppIntents
import Foundation
import HarborCore
import WidgetKit

/// "Hey Siri, connect Harbor", Shortcuts automations, the Action Button,
/// interactive widgets and the Control Center toggle all go through these.
struct ConnectVPNIntent: AppIntent {
    static let title: LocalizedStringResource = "Connect Harbor VPN"
    static let description = IntentDescription("Connects to your last location, or Smart Location.")

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await VPNSwitch.turnOn()
        WidgetCenter.shared.reloadAllTimelines()
        return .result(dialog: "Harbor is connecting.")
    }
}

struct DisconnectVPNIntent: AppIntent {
    static let title: LocalizedStringResource = "Disconnect Harbor VPN"
    static let description = IntentDescription("Turns the VPN off until you connect again.")

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await VPNSwitch.turnOff()
        WidgetCenter.shared.reloadAllTimelines()
        return .result(dialog: "Harbor is disconnected.")
    }
}

/// Backs the widget button and the iOS 18 Control Center toggle.
struct SetVPNIntent: SetValueIntent {
    static let title: LocalizedStringResource = "Turn Harbor VPN On or Off"
    static let isDiscoverable = false

    @Parameter(title: "Connected")
    var value: Bool

    init() {}
    init(_ on: Bool) { value = on }

    func perform() async throws -> some IntentResult {
        if value {
            try await VPNSwitch.turnOn()
        } else {
            try await VPNSwitch.turnOff()
        }
        WidgetCenter.shared.reloadAllTimelines()
        return .result()
    }
}
