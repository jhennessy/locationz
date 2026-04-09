import Foundation
import WatchConnectivity

@MainActor
class PhoneConnectivityService: NSObject, ObservableObject {
    static let shared = PhoneConnectivityService()

    private let session: WCSession

    override init() {
        self.session = WCSession.default
        super.init()

        if WCSession.isSupported() {
            session.delegate = self
            session.activate()
        }
    }

    func sendCredentialsToWatch() {
        guard WCSession.isSupported(), session.isPaired, session.isWatchAppInstalled else { return }

        let api = APIService.shared
        let location = LocationService.shared

        var context: [String: Any] = [
            "serverURL": api.baseURL,
        ]
        if let token = api.token {
            context["token"] = token
        }
        if let user = api.currentUser {
            context["username"] = user.username
        }
        if let deviceId = location.deviceId {
            context["deviceId"] = deviceId
        }

        do {
            try session.updateApplicationContext(context)
            Log.lifecycle.notice("Sent credentials to watch")
        } catch {
            Log.lifecycle.error("Failed to send context to watch: \(error.localizedDescription)")
        }
    }
}

extension PhoneConnectivityService: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        Task { @MainActor in
            Log.lifecycle.notice("WCSession activated: \(activationState.rawValue)")
            self.sendCredentialsToWatch()
        }
    }

    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        Task { @MainActor in
            if message["request"] as? String == "credentials" {
                self.sendCredentialsToWatch()
            }
        }
    }

    nonisolated func sessionWatchStateDidChange(_ session: WCSession) {
        Task { @MainActor in
            if session.isPaired && session.isWatchAppInstalled {
                self.sendCredentialsToWatch()
            }
        }
    }
}
