import Foundation
import EventKit

// meetnext — dump calendar events around now as JSON.
// Usage: meetnext [hoursBack] [hoursForward]

struct EventOut: Codable {
    let title: String
    let start: String
    let end: String
    let epochStart: Double
    let epochEnd: Double
    let allDay: Bool
    let calendar: String
    let account: String
    let location: String?
    let url: String?
    let notes: String?
    let joinUrl: String?
    let status: String
    let availability: String
    let attendeeCount: Int
    let organizer: String?
    let recurring: Bool
}

let args = CommandLine.arguments
let back = Double(args.count > 1 ? args[1] : "") ?? 1.0
let forward = Double(args.count > 2 ? args[2] : "") ?? 48.0
let outPath: String? = (args.count > 1 && args[1] == "--diag")
    ? (args.count > 2 ? args[2] : nil)
    : (args.count > 3 ? args[3] : nil)

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
var deniedReason = "unknown"

if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { ok, err in
        granted = ok
        if let err = err { deniedReason = err.localizedDescription }
        sem.signal()
    }
} else {
    store.requestAccess(to: .event) { ok, err in
        granted = ok
        if let err = err { deniedReason = err.localizedDescription }
        sem.signal()
    }
}
_ = sem.wait(timeout: .now() + 60)

func emit(_ payload: String) {
    if let path = outPath {
        try? payload.write(toFile: path, atomically: true, encoding: .utf8)
    } else {
        FileHandle.standardOutput.write(payload.data(using: .utf8)!)
    }
}

guard granted else {
    let msg = "{\"error\": \"calendar access not granted: \(deniedReason)\"}"
    emit(msg)
    if outPath == nil {
        FileHandle.standardError.write("calendar access not granted: \(deniedReason)\n".data(using: .utf8)!)
    }
    exit(2)
}

if args.count > 1 && args[1] == "--diag" {
    let st = EKEventStore.authorizationStatus(for: .event)
    let statusText: String
    if #available(macOS 14.0, *) {
        switch st {
        case .fullAccess: statusText = "fullAccess"
        case .writeOnly: statusText = "writeOnly"
        case .denied: statusText = "denied"
        case .restricted: statusText = "restricted"
        case .notDetermined: statusText = "notDetermined"
        @unknown default: statusText = "unknown"
        }
    } else {
        statusText = st == .authorized ? "authorized" : "other"
    }
    var rows: [[String: String]] = []
    for c in store.calendars(for: .event) {
        rows.append([
            "title": c.title,
            "source": c.source?.title ?? "",
            "type": String(describing: c.type.rawValue),
            "id": c.calendarIdentifier,
        ])
    }
    let payload: [String: Any] = ["status": statusText, "count": rows.count, "calendars": rows]
    let d = try! JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
    emit(String(data: d, encoding: .utf8) ?? "{}")
    exit(0)
}

let now = Date()
store.refreshSourcesIfNecessary()
let windowStart = now.addingTimeInterval(-back * 3600)
let windowEnd = now.addingTimeInterval(forward * 3600)

let predicate = store.predicateForEvents(withStart: windowStart, end: windowEnd, calendars: nil)
let found = store.events(matching: predicate)

let iso = ISO8601DateFormatter()
iso.timeZone = TimeZone.current

func stripHtml(_ s: String) -> String {
    var out = s.replacingOccurrences(of: "<br\\s*/?>", with: "\n", options: .regularExpression)
    out = out.replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression)
    out = out.replacingOccurrences(of: "&amp;", with: "&")
    out = out.replacingOccurrences(of: "&nbsp;", with: " ")
    return out.trimmingCharacters(in: .whitespacesAndNewlines)
}

let linkPattern = "(https?://[^\\s<>\"]*(zoom\\.us|meet\\.google\\.com|teams\\.microsoft\\.com|webex\\.com|whereby\\.com|meet\\.jitsi\\.org)[^\\s<>\"]*)"
let linkRegex = try! NSRegularExpression(pattern: linkPattern, options: [.caseInsensitive])

func firstLink(in text: String?) -> String? {
    guard let text = text, !text.isEmpty else { return nil }
    let range = NSRange(text.startIndex..., in: text)
    guard let m = linkRegex.firstMatch(in: text, options: [], range: range),
          let r = Range(m.range(at: 1), in: text) else { return nil }
    return String(text[r]).trimmingCharacters(in: CharacterSet(charactersIn: ".,);"))
}

func statusName(_ s: EKEventStatus) -> String {
    switch s {
    case .confirmed: return "confirmed"
    case .tentative: return "tentative"
    case .canceled: return "canceled"
    default: return "none"
    }
}

func availabilityName(_ a: EKEventAvailability) -> String {
    switch a {
    case .busy: return "busy"
    case .free: return "free"
    case .tentative: return "tentative"
    case .unavailable: return "unavailable"
    default: return "unknown"
    }
}

var output: [EventOut] = []
var seen = Set<String>()

for e in found.sorted(by: { $0.startDate < $1.startDate }) {
    let title = e.title ?? ""
    let key = "\(title)|\(e.startDate.timeIntervalSince1970)|\(e.endDate.timeIntervalSince1970)"
    if seen.contains(key) { continue }
    seen.insert(key)

    let notesRaw = e.notes.map(stripHtml)
    let join = e.url?.absoluteString
        ?? firstLink(in: e.location)
        ?? firstLink(in: notesRaw)

    output.append(EventOut(
        title: title,
        start: iso.string(from: e.startDate),
        end: iso.string(from: e.endDate),
        epochStart: e.startDate.timeIntervalSince1970,
        epochEnd: e.endDate.timeIntervalSince1970,
        allDay: e.isAllDay,
        calendar: e.calendar?.title ?? "",
        account: e.calendar?.source?.title ?? "",
        location: e.location,
        url: e.url?.absoluteString,
        notes: notesRaw,
        joinUrl: join,
        status: statusName(e.status),
        availability: availabilityName(e.availability),
        attendeeCount: e.attendees?.count ?? 0,
        organizer: e.organizer?.name,
        recurring: e.hasRecurrenceRules
    ))
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(output)
emit(String(data: data, encoding: .utf8) ?? "[]")
