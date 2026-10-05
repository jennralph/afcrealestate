import ActivityKit
import Foundation

/// Shows connection status on the Lock Screen and in the Dynamic Island.
@MainActor
final class LiveActivityManager {
    private var activity: Activity<HarborActivityAttributes>?

    func show(locationName: String, countryCode: String, since: Date, reconnecting: Bool = false) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let state = HarborActivityAttributes.ContentState(
            locationName: locationName, countryCode: countryCode, connectedSince: since, reconnecting: reconnecting)
        let content = ActivityContent(state: state, staleDate: nil)
        if let activity = activity ?? Activity<HarborActivityAttributes>.activities.first {
            self.activity = activity
            Task { await activity.update(content) }
        } else {
            activity = try? Activity.request(attributes: HarborActivityAttributes(), content: content)
        }
    }

    func end() {
        let all = Activity<HarborActivityAttributes>.activities
        activity = nil
        Task {
            for a in all { await a.end(nil, dismissalPolicy: .immediate) }
        }
    }
}
