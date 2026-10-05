import StoreKit
import SwiftUI

/// Apple's own subscription sheet: prices, trial terms and renewal dates come
/// straight from the App Store, so there are no hidden terms and nothing to
/// get wrong. Harbor charges the same price at every renewal.
struct PaywallView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        SubscriptionStoreView(productIDs: AppConfig.productIDs) {
            VStack(spacing: 14) {
                Image(systemName: "sparkles").font(.system(size: 40)).foregroundStyle(Color.harborAccent)
                Text("Harbor Plus").font(.largeTitle.bold())
                VStack(alignment: .leading, spacing: 10) {
                    perk("globe", "Every location, including streaming-optimized servers")
                    perk("iphone.gen3", "Up to 7 devices on one account")
                    perk("speedometer", "Plus-only servers, so less crowding at peak times")
                    perk("tag", "Same price every renewal. No intro-price surprises.")
                    perk("xmark.circle", "Cancel any time in two taps from Settings")
                }
                .padding(.horizontal, 8)
            }
            .padding(.vertical, 24)
            .padding(.horizontal)
        }
        .subscriptionStoreControlStyle(.prominentPicker)
        .storeButton(.visible, for: .restorePurchases)
        .subscriptionStorePolicyDestination(url: AppConfig.privacyPolicyURL, for: .privacyPolicy)
        .subscriptionStorePolicyDestination(url: AppConfig.termsURL, for: .termsOfService)
        .onInAppPurchaseCompletion { _, result in
            if case .success(.success(let verification)) = result {
                await model.purchases.handle(verification)
            }
        }
        .overlay(alignment: .topTrailing) {
            Button { dismiss() } label: {
                Image(systemName: "xmark.circle.fill").font(.title2).symbolRenderingMode(.hierarchical)
            }
            .padding()
            .accessibilityLabel("Close")
        }
    }

    func perk(_ icon: String, _ text: String) -> some View {
        Label {
            Text(text).font(.callout)
        } icon: {
            Image(systemName: icon).foregroundStyle(Color.harborAccent).frame(width: 26)
        }
    }
}
