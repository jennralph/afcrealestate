import Foundation
import Security

/// Keychain items shared between the app and the packet tunnel. Items are
/// readable after the first unlock (so on-demand can connect while the phone
/// is locked) and never leave this device, not even in encrypted backups.
enum SharedKeychain {
    enum Item: String {
        case wireGuardKey = "wireguard-private-key"
        case accountNumber = "account-number"
    }

    private static let service = "net.harborvpn"

    private static func base(_ item: Item) -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: item.rawValue,
        ]
        if !AppConfig.keychainGroup.isEmpty {
            q[kSecAttrAccessGroup as String] = AppConfig.keychainGroup
        }
        return q
    }

    @discardableResult
    static func set(_ data: Data, for item: Item) -> Bool {
        let query = base(item)
        let attrs: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        if status == errSecItemNotFound {
            return SecItemAdd(query.merging(attrs) { $1 } as CFDictionary, nil) == errSecSuccess
        }
        return status == errSecSuccess
    }

    static func data(for item: Item) -> Data? {
        var q = base(item)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? Data
    }

    /// The handle NETunnelProviderProtocol.passwordReference needs.
    static func persistentReference(for item: Item) -> Data? {
        var q = base(item)
        q[kSecReturnPersistentRef as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? Data
    }

    static func data(forPersistentReference ref: Data) -> Data? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecValuePersistentRef as String: ref,
            kSecReturnData as String: true,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? Data
    }

    static func delete(_ item: Item) {
        SecItemDelete(base(item) as CFDictionary)
    }
}
