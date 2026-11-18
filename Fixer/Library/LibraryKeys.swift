import CryptoKit
import Foundation
import LocalAuthentication
import Security

/// A random per-item key (or the master key), sealed so only the right key opens it.
/// "se": sealed to the device's Secure Enclave key (needs Face ID or the passcode to open).
/// "k": sealed under the master key (fast, needs the Library open). "s": sealed under a plain keychain key
/// (used only when the Secure Enclave can't be used yet); the next unlock re-seals it as "k".
struct LibraryWrapped: Codable {
    var v = 1
    var w: String
    var eph: Data?
    var box: Data
}

/// Keys for the Library: a Secure Enclave key that new items are sealed to without any prompt (so chat and Make can
/// add while the Library is locked), and a master key that opens after Face ID or the passcode, once.
enum LibraryKeys {
    private static let service = "org.immortaldual.fixer"
    private static let seAccount = "library-se"
    private static let pubAccount = "library-pub"
    private static let masterAccount = "library-master"
    private static let softAccount = "library-soft"
    private static let info = Data("fixer-lib-v1".utf8)

    // MARK: keychain

    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    /// nil when the item isn't there; throws when the keychain can't be read right now (never treat that as "missing").
    private static func read(_ account: String) throws -> Data? {
        var q = query(account)
        q[kSecReturnData as String] = true
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = out as? Data else { throw LibraryError.keys }
        return data
    }

    private static func write(_ account: String, _ data: Data, accessible: CFString) throws {
        SecItemDelete(query(account) as CFDictionary)
        var q = query(account)
        q[kSecValueData as String] = data
        q[kSecAttrAccessible as String] = accessible
        guard SecItemAdd(q as CFDictionary, nil) == errSecSuccess else { throw LibraryError.keys }
    }

    /// A fresh install must not inherit keys from an earlier install (the keychain outlives app deletion).
    static func forget() {
        for a in [seAccount, pubAccount, masterAccount, softAccount] { SecItemDelete(query(a) as CFDictionary) }
    }

    // MARK: sealing

    private static func bytes(_ key: SymmetricKey) -> Data { key.withUnsafeBytes { Data($0) } }

    static func seal(_ data: Data, with key: SymmetricKey) throws -> Data {
        guard let combined = try AES.GCM.seal(data, using: key).combined else { throw LibraryError.damaged }
        return combined
    }

    static func open(_ data: Data, with key: SymmetricKey) throws -> Data {
        try AES.GCM.open(try AES.GCM.SealedBox(combined: data), using: key)
    }

    private static func wrapK(_ secret: SymmetricKey, under key: SymmetricKey, tag: String) throws -> LibraryWrapped {
        LibraryWrapped(w: tag, eph: nil, box: try seal(bytes(secret), with: key))
    }

    private static func derived(_ shared: SharedSecret, salt: UUID) -> SymmetricKey {
        shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: Data(salt.uuidString.utf8), sharedInfo: info,
                                       outputByteCount: 32)
    }

    private static func wrapSE(_ secret: SymmetricKey, id: UUID, to pub: P256.KeyAgreement.PublicKey) throws -> LibraryWrapped {
        let eph = P256.KeyAgreement.PrivateKey()
        let shared = try eph.sharedSecretFromKeyAgreement(with: pub)
        let box = try seal(bytes(secret), with: derived(shared, salt: id))
        return LibraryWrapped(w: "se", eph: eph.publicKey.rawRepresentation, box: box)
    }

    private static func unwrapSE(_ w: LibraryWrapped, id: UUID,
                                 with priv: SecureEnclave.P256.KeyAgreement.PrivateKey) throws -> SymmetricKey {
        guard let raw = w.eph else { throw LibraryError.damaged }
        let eph = try P256.KeyAgreement.PublicKey(rawRepresentation: raw)
        let shared = try priv.sharedSecretFromKeyAgreement(with: eph)
        return SymmetricKey(data: try open(w.box, with: derived(shared, salt: id)))
    }

    // MARK: the device key and the plain fallback key

    /// The device key's public half. Making the key needs no prompt; using it later does.
    private static func devicePublicKey() throws -> P256.KeyAgreement.PublicKey {
        if let raw = try read(pubAccount) { return try P256.KeyAgreement.PublicKey(rawRepresentation: raw) }
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                                                           [.privateKeyUsage, .userPresence], &error) else {
            throw LibraryError.keys
        }
        let key = try SecureEnclave.P256.KeyAgreement.PrivateKey(accessControl: access)
        try write(seAccount, key.dataRepresentation, accessible: kSecAttrAccessibleWhenUnlockedThisDeviceOnly)
        try write(pubAccount, key.publicKey.rawRepresentation, accessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly)
        return key.publicKey
    }

    private static func softKey() throws -> SymmetricKey {
        if let raw = try read(softAccount) { return SymmetricKey(data: raw) }
        let key = SymmetricKey(size: .bits256)
        try write(softAccount, bytes(key), accessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly)
        return key
    }

    /// The device key itself, opened with the context that already passed Face ID or the passcode.
    static func devicePrivateKey(context: LAContext) throws -> SecureEnclave.P256.KeyAgreement.PrivateKey? {
        guard SecureEnclave.isAvailable else { return nil }
        _ = try devicePublicKey()
        guard let blob = try read(seAccount) else { throw LibraryError.keys }
        return try SecureEnclave.P256.KeyAgreement.PrivateKey(dataRepresentation: blob, authenticationContext: context)
    }

    // MARK: per-item keys

    /// Seals a new item's key. With the master key at hand it is sealed straight under it; otherwise to the device
    /// key (no prompt), and if that can't be made yet (no passcode), under the plain fallback key.
    static func wrapNew(_ dek: SymmetricKey, id: UUID, master: SymmetricKey?) throws -> LibraryWrapped {
        if let master { return try wrapK(dek, under: master, tag: "k") }
        if SecureEnclave.isAvailable, let pub = try? devicePublicKey() { return try wrapSE(dek, id: id, to: pub) }
        return try wrapK(dek, under: try softKey(), tag: "s")
    }

    /// Opens an item's key. "se" needs the device key; the others need only the keys passed in.
    static func unwrap(_ w: LibraryWrapped, id: UUID, master: SymmetricKey,
                       priv: SecureEnclave.P256.KeyAgreement.PrivateKey?) -> SymmetricKey? {
        switch w.w {
        case "k": return (try? open(w.box, with: master)).map { SymmetricKey(data: $0) }
        case "se": return priv.flatMap { try? unwrapSE(w, id: id, with: $0) }
        case "s":
            guard let soft = try? softKey() else { return nil }
            return (try? open(w.box, with: soft)).map { SymmetricKey(data: $0) }
        default: return nil
        }
    }

    static func rewrap(_ dek: SymmetricKey, under master: SymmetricKey) -> LibraryWrapped? {
        try? wrapK(dek, under: master, tag: "k")
    }

    // MARK: the master key

    private static let masterSalt = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!

    /// The master key: made on the first unlock, stored sealed to the device key. Call only after the context has
    /// passed Face ID or the passcode.
    static func loadOrCreateMaster(context: LAContext) throws -> (master: SymmetricKey, priv: SecureEnclave.P256.KeyAgreement.PrivateKey?) {
        guard let priv = try devicePrivateKey(context: context) else {
            return (try softKey(), nil)                    // no Secure Enclave (a simulator): the plain key is the master
        }
        if let blob = try read(masterAccount) {
            let w = try JSONDecoder().decode(LibraryWrapped.self, from: blob)
            return (try unwrapSE(w, id: masterSalt, with: priv), priv)
        }
        let master = SymmetricKey(size: .bits256)
        let w = try wrapSE(master, id: masterSalt, to: try devicePublicKey())
        try write(masterAccount, try JSONEncoder().encode(w), accessible: kSecAttrAccessibleWhenUnlockedThisDeviceOnly)
        return (master, priv)
    }
}
