// MitsyncCalendar: read Apple Calendar events for mitsync, from an app bundle.
//
// Why an app: macOS grants Calendar access to the *responsible* process. For
// anything the OpenClaw gateway runs, that is its bare `node` binary, which
// has no usage string, so the request is refused without a prompt. An app
// launched through LaunchServices (`open`) is responsible for itself, can show
// the prompt once, and keeps the grant. `bin/mitsync-calendar` does the launch.
//
// It accepts ical-guy's read-only query (`events list --from D --to D
// --format json --group-by none`) and prints ical-guy's JSON event shape, so
// `mitsync/schedule/calendar.py` needs no adapter. Output goes to files named
// by --out/--err/--status, because `open` does not forward stdout.
//
// READ-ONLY. Only `requestAccess`, `calendars(for:)`, `predicateForEvents`
// and `events(matching:)` are called. tests/test_calendar_helper.py fails the
// build if an EventKit write API ever appears in this file.

import EventKit
import Foundation

var outPath: String?
var errPath: String?
var statusPath: String?
var fromDay: String?
var toDay: String?

func emit(_ text: String, to path: String?, fallback: UnsafeMutablePointer<FILE>) {
    if let path = path {
        try? text.write(toFile: path, atomically: true, encoding: .utf8)
    } else {
        fputs(text, fallback)
    }
}

func finish(_ code: Int32, stdout: String = "", stderr: String = "") -> Never {
    emit(stdout, to: outPath, fallback: Foundation.stdout)
    emit(stderr, to: errPath, fallback: Foundation.stderr)
    if let statusPath = statusPath {
        try? "\(code)".write(toFile: statusPath, atomically: true, encoding: .utf8)
    }
    exit(code)
}

// -- arguments: ical-guy's query plus the three output files ---------------
var args = Array(CommandLine.arguments.dropFirst())
var positional: [String] = []
while !args.isEmpty {
    let arg = args.removeFirst()
    let takesValue = ["--from", "--to", "--format", "--group-by", "--out", "--err", "--status"]
    if takesValue.contains(arg) {
        guard !args.isEmpty else { finish(64, stderr: "Error: \(arg) needs a value\n") }
        let value = args.removeFirst()
        switch arg {
        case "--from": fromDay = value
        case "--to": toDay = value
        case "--out": outPath = value
        case "--err": errPath = value
        case "--status": statusPath = value
        case "--format" where value != "json":
            finish(64, stderr: "Error: only --format json is supported\n")
        case "--group-by" where value != "none":
            finish(64, stderr: "Error: only --group-by none is supported\n")
        default: break
        }
    } else if arg.hasPrefix("-psn_") {
        continue  // LaunchServices process serial number, on older systems
    } else {
        positional.append(arg)
    }
}

// Opened by hand (Finder, or `open` with no query): just ask for access.
let grantOnly = positional.isEmpty && fromDay == nil
if !grantOnly && positional != ["events", "list"] {
    finish(64, stderr: "Error: usage: MitsyncCalendar events list --from YYYY-MM-DD --to YYYY-MM-DD\n")
}

let localDay = DateFormatter()
localDay.calendar = Calendar(identifier: .gregorian)
localDay.locale = Locale(identifier: "en_US_POSIX")
localDay.timeZone = TimeZone.current
localDay.dateFormat = "yyyy-MM-dd"

func parseDay(_ text: String?) -> Date? {
    guard let text = text else { return nil }
    let today = Calendar.current.startOfDay(for: Date())
    switch text {
    case "today": return today
    case "tomorrow": return Calendar.current.date(byAdding: .day, value: 1, to: today)
    default: return localDay.date(from: text)
    }
}

// -- access ----------------------------------------------------------------
let store = EKEventStore()
let waiter = DispatchSemaphore(value: 0)
var granted = false
store.requestAccess(to: .event) { ok, _ in
    granted = ok
    waiter.signal()
}
waiter.wait()

let deniedMessage = """
    Error: Calendar access denied. Grant access in:
    System Settings > Privacy & Security > Calendars (MitsyncCalendar)

    """
if !granted { finish(1, stderr: deniedMessage) }
if grantOnly { finish(0, stderr: "MitsyncCalendar has Calendar access.\n") }

// -- query: inclusive whole local days, like ical-guy ------------------------
guard let start = parseDay(fromDay) else {
    finish(1, stderr: "Error: Invalid date format: '\(fromDay ?? "")'. Use YYYY-MM-DD.\n")
}
guard let lastDay = parseDay(toDay ?? fromDay) else {
    finish(1, stderr: "Error: Invalid date format: '\(toDay ?? "")'. Use YYYY-MM-DD.\n")
}
let end = Calendar.current.date(byAdding: .day, value: 1, to: lastDay)!

let iso = ISO8601DateFormatter()
iso.timeZone = TimeZone(identifier: "UTC")

let predicate = store.predicateForEvents(
    withStart: start, end: end, calendars: store.calendars(for: .event))
let rows: [[String: Any]] = store.events(matching: predicate).map { event in
    var row: [String: Any] = [
        "id": event.calendarItemExternalIdentifier ?? event.eventIdentifier ?? "",
        "title": event.title ?? "",
        "startDate": iso.string(from: event.startDate),
        "endDate": iso.string(from: event.endDate),
        "isAllDay": event.isAllDay,
        "calendar": [
            "title": event.calendar.title,
            "source": event.calendar.source?.title ?? "",
        ],
    ]
    if let location = event.location, !location.isEmpty { row["location"] = location }
    if let notes = event.notes, !notes.isEmpty { row["notes"] = notes }
    if let zone = event.timeZone { row["timeZone"] = zone.identifier }
    return row
}

guard let data = try? JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys]),
    let json = String(data: data, encoding: .utf8)
else {
    finish(70, stderr: "Error: could not encode events as JSON\n")
}
finish(0, stdout: json + "\n")
