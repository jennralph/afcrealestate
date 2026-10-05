import CryptoKit
import Foundation

/// This iPhone's WireGuard key pair. The private half is generated on the
/// phone and never leaves it; only the public key is sent to Harbor.
enum DeviceKey {
    struct Pair {
        let privateKey: Data
        var publicKeyBase64: String {
            // Force-try is safe: privateKey is always 32 bytes we generated.
            (try! Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateKey)).publicKey.rawRepresentation.base64EncodedString()
        }
    }

    static func generate() -> Pair {
        Pair(privateKey: Curve25519.KeyAgreement.PrivateKey().rawRepresentation)
    }

    static var current: Pair? {
        SharedKeychain.data(for: .wireGuardKey).map(Pair.init(privateKey:))
    }

    /// Stores the pair and returns the Keychain reference the tunnel reads.
    static func save(_ pair: Pair) -> Data? {
        guard SharedKeychain.set(pair.privateKey, for: .wireGuardKey) else { return nil }
        return SharedKeychain.persistentReference(for: .wireGuardKey)
    }

    static var reference: Data? { SharedKeychain.persistentReference(for: .wireGuardKey) }
}
