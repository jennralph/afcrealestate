import HarborCore
import SwiftUI

struct OnboardingView: View {
    @Environment(AppModel.self) private var model
    @State private var signingIn = false

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            Image(systemName: "lock.shield.fill")
                .font(.system(size: 88))
                .foregroundStyle(Color.harborAccent.gradient)
                .padding(.bottom, 24)
            Text("Private by default.")
                .font(.largeTitle.bold())
                .multilineTextAlignment(.center)
            Text("One tap protects everything you do on this iPhone. No email, no tracking, no logs.")
                .font(.body).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.top, 10)
                .padding(.horizontal, 24)

            VStack(alignment: .leading, spacing: 14) {
                feature("bolt.fill", "WireGuard speed, built for battery life")
                feature("hand.raised.fill", "Blocks ads, trackers and malware")
                feature("wifi", "Auto-connects on public Wi-Fi")
                feature("infinity", "Free plan with unlimited data")
            }
            .padding(.top, 32)
            Spacer()

            Button {
                Task { await model.createAccount() }
            } label: {
                Group {
                    if model.busy { ProgressView().tint(.white) } else { Text("Get started — no sign-up") }
                }
                .font(.headline)
                .frame(maxWidth: .infinity, minHeight: 54)
            }
            .buttonStyle(.borderedProminent)
            .buttonBorderShape(.roundedRectangle(radius: 16))
            .disabled(model.busy)

            Button("I have an account number") { signingIn = true }
                .padding(.top, 14)
                .padding(.bottom, 8)
        }
        .padding(24)
        .sheet(isPresented: $signingIn) { SignInView() }
    }

    func feature(_ icon: String, _ text: String) -> some View {
        Label {
            Text(text)
        } icon: {
            Image(systemName: icon).foregroundStyle(Color.harborAccent).frame(width: 28)
        }
        .font(.callout)
    }
}

struct SignInView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var number = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("0000 0000 0000 0000", text: $number)
                        .keyboardType(.numberPad)
                        .textContentType(.password)
                        .font(.title3.monospacedDigit())
                        .focused($focused)
                        .onChange(of: number) { _, new in
                            let digits = new.filter(\.isNumber).prefix(AccountNumber.length)
                            let grouped = AccountNumber.grouped(String(digits))
                            if grouped != new { number = grouped }
                        }
                } footer: {
                    Text("Enter the 16-digit number you saved when you created your account.")
                }
            }
            .navigationTitle("Sign In")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Sign In") {
                        Task { if await model.signIn(number) { dismiss() } }
                    }
                    .disabled(AccountNumber.normalize(number) == nil || model.busy)
                }
            }
            .onAppear { focused = true }
        }
        .presentationDetents([.medium])
    }
}

/// Shown once after sign-up. The number is the only way back into the account.
struct AccountNumberRevealView: View {
    @Environment(\.dismiss) private var dismiss
    let number: String
    @State private var copied = false

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "key.fill").font(.system(size: 44)).foregroundStyle(Color.harborAccent)
            Text("Your account number").font(.title2.bold())
            Text(AccountNumber.grouped(number))
                .font(.title.monospacedDigit().weight(.semibold))
                .textSelection(.enabled)
                .padding()
                .frame(maxWidth: .infinity)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
            Text("This is your login — there's no email or password. Save it in your password manager to use Harbor on other devices or to restore your plan.")
                .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
            Button(copied ? "Copied" : "Copy") {
                UIPasteboard.general.string = number
                copied = true
            }
            .buttonStyle(.bordered)
            Spacer()
            Button {
                dismiss()
            } label: {
                Text("I've saved it").font(.headline).frame(maxWidth: .infinity, minHeight: 50)
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(24)
        .interactiveDismissDisabled()
    }
}
