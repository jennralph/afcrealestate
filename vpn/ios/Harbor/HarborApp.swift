import AppIntents
import SwiftUI

@main
struct HarborApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .task { await model.bootstrap() }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active, model.phase == .ready {
                        Task { await model.refresh() }
                    }
                }
                .tint(.harborAccent)
        }
    }
}

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Group {
            switch model.phase {
            case .launching:
                ProgressView().controlSize(.large)
            case .onboarding:
                OnboardingView()
            case .ready:
                HomeView()
            }
        }
        .animation(.default, value: model.phase)
        .alert(item: $model.alert) { item in
            Alert(title: Text(item.title), message: Text(item.message))
        }
        .sheet(isPresented: $model.showPaywall) { PaywallView() }
        .sheet(item: Binding(
            get: { model.newAccountNumber.map(IdentifiedString.init) },
            set: { model.newAccountNumber = $0?.value }
        )) { item in
            AccountNumberRevealView(number: item.value)
        }
    }
}

struct IdentifiedString: Identifiable {
    let value: String
    var id: String { value }
}

struct HarborShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: ConnectVPNIntent(), phrases: ["Connect \(.applicationName)", "Turn on \(.applicationName)"],
                    shortTitle: "Connect", systemImageName: "lock.shield")
        AppShortcut(intent: DisconnectVPNIntent(), phrases: ["Disconnect \(.applicationName)", "Turn off \(.applicationName)"],
                    shortTitle: "Disconnect", systemImageName: "shield.slash")
    }
}

extension Color {
    static let harborAccent = Color(red: 0.16, green: 0.45, blue: 0.98)
    static let harborProtected = Color(red: 0.13, green: 0.75, blue: 0.47)
}
