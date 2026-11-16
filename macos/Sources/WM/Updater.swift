import Foundation
import Sparkle

/// In-app updates through Sparkle. A release build carries its own feed (`SUFeedURL` in Info.plist); the
/// development build has none, so its updater never starts and it can't offer to replace itself.
///
/// For testing an update end to end, the environment variable `WM_UPDATE_FEED_URL` overrides the feed and also
/// switches the updater on, for that one process (see the README). It is not a user default on purpose: the
/// defaults domain is shared with the installed app, so a leftover default would point the real app at a test feed.
@MainActor @Observable
final class Updater: NSObject, SPUUpdaterDelegate {
    static let shared = Updater()

    /// Drives the menu item: false until the updater is running, and while a check or install is under way.
    private(set) var canCheckForUpdates = false
    @ObservationIgnored private var observation: NSKeyValueObservation?
    @ObservationIgnored private var controller: SPUStandardUpdaterController!

    /// `WM_UPDATE_FEED_URL`, when set and not empty.
    nonisolated private static var feedOverride: String? {
        ProcessInfo.processInfo.environment["WM_UPDATE_FEED_URL"].flatMap { $0.isEmpty ? nil : $0 }
    }
    private static var hasFeed: Bool {
        feedOverride != nil || Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") != nil
    }

    private override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self,
                                                  userDriverDelegate: nil)
    }

    /// Called once at launch. Does nothing without a feed.
    func start() {
        guard Self.hasFeed else { return }
        controller.startUpdater()
        observation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
            let can = updater.canCheckForUpdates
            Task { @MainActor in self?.canCheckForUpdates = can }
        }
    }

    func checkForUpdates() { controller.checkForUpdates(nil) }

    nonisolated func feedURLString(for updater: SPUUpdater) -> String? {
        Updater.feedOverride
    }
}
