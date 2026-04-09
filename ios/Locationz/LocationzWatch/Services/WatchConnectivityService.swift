import Foundation
import WatchConnectivity
import os

@MainActor
class WatchConnectivityService: NSObject, ObservableObject {
    static let shared = WatchConnectivityService()

    private static let logger = Logger(subsystem: "ch.codelook.locationz.watch", category: "connectivity")

    @Published var token: String? {
        didSet { UserDefaults.standard.set(token, forKey: "watch_auth_token") }
    }
    @Published var serverURL: String {
        didSet { UserDefaults.standard.set(serverURL, forKey: "watch_server_url") }
    }
    @Published var deviceId: Int? {
        didSet {
            if let id = deviceId {
                UserDefaults.standard.set(id, forKey: "watch_device_id")
            } else {
                UserDefaults.standard.removeObject(forKey: "watch_device_id")
            }
        }
    }
    @Published var username: String?
    @Published var isPhoneReachable = false

    private let session: WCSession

    override init() {
        self.session = WCSession.default
        self.token = UserDefaults.standard.string(forKey: "watch_auth_token")
        self.serverURL = UserDefaults.standard.string(forKey: "watch_server_url") ?? "https://locationz.codelook.ch"
        self.deviceId = UserDefaults.standard.object(forKey: "watch_device_id") as? Int
        super.init()

        if WCSession.isSupported() {
            session.delegate = self
            session.activate()
        }
    }

    func requestCredentials() {
        guard session.isReachable else { return }
        session.sendMessage(["request": "credentials"], replyHandler: nil)
    }

    private func handleContext(_ context: [String: Any]) {
        if let token = context["token"] as? String {
            self.token = token
        }
        if let url = context["serverURL"] as? String {
            self.serverURL = url
        }
        if let id = context["deviceId"] as? Int {
            self.deviceId = id
        }
        if let name = context["username"] as? String {
            self.username = name
        }

        Self.logger.notice("Received context — token: \(self.token != nil), deviceId: \(self.deviceId ?? -1)")
    }
}

extension WatchConnectivityService: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        Task { @MainActor in
            Self.logger.notice("WCSession activated: \(activationState.rawValue)")
            self.isPhoneReachable = session.isReachable
            if session.isReachable {
                self.requestCredentials()
            }
            // Load any previously received application context
            let ctx = session.receivedApplicationContext
            if !ctx.isEmpty {
                self.handleContext(ctx)
            }
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        Task { @MainActor in
            self.handleContext(applicationContext)
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        Task { @MainActor in
            self.handleContext(message)
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        Task { @MainActor in
            self.isPhoneReachable = session.isReachable
            if session.isReachable && self.token == nil {
                self.requestCredentials()
            }
        }
    }
}
