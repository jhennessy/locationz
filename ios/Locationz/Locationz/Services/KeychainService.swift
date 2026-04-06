import Foundation
import Security

/// Stores and retrieves login credentials from the iOS Keychain so the app
/// can silently re-authenticate when the server token expires.
enum KeychainService {
    private static let service = "ch.codelook.locationz"
    private static let usernameKey = "login_username"
    private static let passwordKey = "login_password"

    // MARK: - Public API

    static func storeCredentials(username: String, password: String) {
        set(key: usernameKey, value: username)
        set(key: passwordKey, value: password)
    }

    static func loadCredentials() -> (username: String, password: String)? {
        guard let username = get(key: usernameKey),
              let password = get(key: passwordKey) else {
            return nil
        }
        return (username, password)
    }

    static func clearCredentials() {
        delete(key: usernameKey)
        delete(key: passwordKey)
    }

    // MARK: - Keychain helpers

    private static func set(key: String, value: String) {
        guard let data = value.data(using: .utf8) else { return }

        // Delete any existing item first
        delete(key: key)

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        SecItemAdd(query as CFDictionary, nil)
    }

    private static func get(key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func delete(key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
