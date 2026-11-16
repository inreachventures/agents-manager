// Checks a release disk image the way the app that installs it will, before CI publishes it:
//   swift macos/scripts/check-update.swift DMG CHIP VERSION SIGNATURE
// SIGNATURE is the EdDSA signature sign_update printed for DMG. The app inside must read this chip's feed
// (appcast-CHIP.xml) and be VERSION, and SIGNATURE must verify with the app's own public key (SUPublicEDKey).
// So a signing secret that doesn't match the key in the app fails the release, rather than shipping an update
// that every copy of the app would refuse to install.
import CryptoKit
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("check-update: \(message)\n".utf8))
    exit(1)
}

func hdiutil(_ args: [String]) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/hdiutil")
    process.arguments = args
    process.standardOutput = FileHandle.nullDevice
    guard (try? process.run()) != nil else { return false }
    process.waitUntilExit()
    return process.terminationStatus == 0
}

let args = Array(CommandLine.arguments.dropFirst())
guard args.count == 4 else { fail("usage: check-update.swift DMG CHIP VERSION SIGNATURE") }
let (dmg, chip, version, signature) = (args[0], args[1], args[2], args[3])

let mount = FileManager.default.temporaryDirectory.appendingPathComponent("check-update-\(UUID().uuidString)")
guard hdiutil(["attach", "-readonly", "-nobrowse", "-mountpoint", mount.path, dmg]) else {
    fail("can't mount \(dmg)")
}
let plistURL = mount.appendingPathComponent("Workstreams.app/Contents/Info.plist")
let plist = (try? Data(contentsOf: plistURL))
    .flatMap { try? PropertyListSerialization.propertyList(from: $0, format: nil) as? [String: Any] }
_ = hdiutil(["detach", mount.path])
guard let plist else { fail("no Workstreams.app/Contents/Info.plist in \(dmg)") }

var problems: [String] = []
let feed = plist["SUFeedURL"] as? String ?? ""
if !feed.hasSuffix("/appcast-\(chip).xml") { problems.append("the app's feed is '\(feed)', not appcast-\(chip).xml") }
for key in ["CFBundleVersion", "CFBundleShortVersionString"] where plist[key] as? String != version {
    problems.append("the app's \(key) is '\(plist[key] as? String ?? "")', not \(version)")
}
let publicKey = (plist["SUPublicEDKey"] as? String).flatMap { Data(base64Encoded: $0) }
    .flatMap { try? Curve25519.Signing.PublicKey(rawRepresentation: $0) }
if let publicKey {
    guard let image = try? Data(contentsOf: URL(fileURLWithPath: dmg), options: .mappedIfSafe) else {
        fail("can't read \(dmg)")
    }
    if !(Data(base64Encoded: signature).map { publicKey.isValidSignature($0, for: image) } ?? false) {
        problems.append("the signature doesn't verify with the app's public key (SUPublicEDKey): is the signing "
                        + "secret the private half of that key?")
    }
} else {
    problems.append("the app has no valid SUPublicEDKey")
}
guard problems.isEmpty else { fail("\(dmg):\n  " + problems.joined(separator: "\n  ")) }
print("\(dmg): \(chip) feed, version \(version), signature matches the app's key")
