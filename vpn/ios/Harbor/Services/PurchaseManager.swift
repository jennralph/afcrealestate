import Foundation
import StoreKit

/// Hands every verified StoreKit 2 transaction (new purchases, renewals,
/// restores, purchases made on another device) to Harbor's server, and only
/// finishes it once the server has applied it, so nothing is lost if the
/// network drops mid-purchase.
@MainActor
final class PurchaseManager {
    /// Returns true when the server accepted the signed transaction.
    var deliver: ((String) async -> Bool)?
    private var updates: Task<Void, Never>?

    func start() {
        updates?.cancel()
        updates = Task { [weak self] in
            for await result in Transaction.updates {
                await self?.handle(result)
            }
        }
    }

    /// Re-sends current entitlements, e.g. after signing in on a new phone.
    func syncEntitlements() async {
        for await result in Transaction.currentEntitlements {
            await handle(result)
        }
    }

    func handle(_ result: VerificationResult<Transaction>) async {
        guard case .verified(let tx) = result, let deliver else { return }
        if await deliver(result.jwsRepresentation) {
            await tx.finish()
        }
    }

    func restore() async throws {
        try await AppStore.sync()
        await syncEntitlements()
    }
}
