import HarborCore
import StoreKit
import SwiftUI

struct AccountView: View {
    @Environment(AppModel.self) private var model
    @State private var reveal = false
    @State private var copied = false
    @State private var confirmSignOut = false
    @State private var manageSubscription = false

    var body: some View {
        List {
            Section {
                if let number = model.accountNumber {
                    Button {
                        reveal.toggle()
                    } label: {
                        LabeledContent("Account number") {
                            Text(reveal ? AccountNumber.grouped(number) : AccountNumber.masked(number))
                                .monospacedDigit()
                        }
                    }
                    .foregroundStyle(.primary)
                    Button(copied ? "Copied" : "Copy account number") {
                        UIPasteboard.general.setItems([[UIPasteboard.typeAutomatic: number]],
                                                      options: [.expirationDate: Date.now.addingTimeInterval(120)])
                        copied = true
                    }
                }
            } footer: {
                Text("This number is your login. Keep it somewhere safe; we can't recover it for you. Copies expire from the clipboard after 2 minutes.")
            }

            Section("Plan") {
                LabeledContent("Current plan", value: model.tier == .paid ? "Harbor Plus" : "Free")
                if let until = model.account?.paidUntil, model.tier == .paid {
                    LabeledContent("Renews or ends", value: until.formatted(date: .abbreviated, time: .omitted))
                }
                if model.tier == .paid {
                    Button("Manage subscription") { manageSubscription = true }
                } else {
                    Button("Upgrade to Harbor Plus") { model.showPaywall = true }
                }
                Button("Restore purchases") {
                    Task { try? await model.purchases.restore() }
                }
            }

            Section {
                ForEach(model.account?.devices ?? []) { device in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(device.name)
                            Text("Added \(device.created.formatted(date: .abbreviated, time: .omitted))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if device.id == model.thisDevice?.id {
                            Text("This iPhone").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .swipeActions {
                        if device.id != model.thisDevice?.id {
                            Button("Remove", role: .destructive) { Task { await model.removeDevice(device) } }
                        }
                    }
                }
            } header: {
                Text("Devices (\(model.account?.devices.count ?? 0) of \(model.account?.maxDevices ?? 1))")
            } footer: {
                Text("Swipe to remove a device you no longer use.")
            }

            Section {
                Button("Sign out", role: .destructive) { confirmSignOut = true }
            }
        }
        .navigationTitle("Account")
        .refreshable { await model.refreshAccount() }
        .manageSubscriptionsSheet(isPresented: $manageSubscription)
        .confirmationDialog("Sign out of Harbor?", isPresented: $confirmSignOut, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { Task { await model.signOut() } }
        } message: {
            Text("You'll need your account number to sign back in. The VPN will be turned off.")
        }
    }
}
