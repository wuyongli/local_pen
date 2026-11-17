import AppKit
import ApplicationServices
import Foundation

let penBundleID = "dev.pencil.desktop"

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func windows(_ app: AXUIElement) -> [AXUIElement]? {
    attribute(app, kAXWindowsAttribute) as? [AXUIElement]
}

let documentAttributes = [kAXDocumentAttribute, kAXURLAttribute, kAXTitleAttribute, "AXRepresentedFilename"]

func documentURL(_ value: CFTypeRef) -> URL? {
    let url: URL?
    if let value = value as? String {
        url = value.hasPrefix("/") ? URL(fileURLWithPath: value) : URL(string: value)
    } else {
        url = value as? URL
    }
    guard let url, url.isFileURL else { return nil }
    return url.standardizedFileURL.resolvingSymlinksInPath()
}

func documentAttributesForWindow(_ window: AXUIElement) -> [(String, CFTypeRef)] {
    // Electron can expose the represented URL on the native container immediately inside the window.
    let children = attribute(window, kAXChildrenAttribute) as? [AXUIElement] ?? []
    return ([window] + children).flatMap { element in
        documentAttributes.compactMap { name in attribute(element, name).map { (name, $0) } }
    }
}

func matches(_ window: AXUIElement, _ target: URL) -> Bool {
    documentAttributesForWindow(window).contains { _, value in documentURL(value)?.path == target.path }
}

func verifySaved(_ window: AXUIElement, filename: String) {
    if attribute(window, "AXEdited") as? Bool == true {
        fail("Pen has unsaved changes in the working copy. Save them in Pen and retry.")
    }
    guard (attribute(window, "AXSheets") as? [AXUIElement] ?? []).isEmpty else {
        fail("Pen has an open dialog on the working copy. Resolve it and retry.")
    }
    // Pen 1.2.5 reports the unsaved indicator in its web title bar.
    var pending = [window]
    var checked = 0
    var foundSavedTitle = false
    while let element = pending.popLast(), checked < 240 {
        checked += 1
        let role = attribute(element, kAXRoleAttribute) as? String
        if role == kAXStaticTextRole {
            for name in [kAXValueAttribute, kAXTitleAttribute] {
                guard let value = attribute(element, name) as? String else { continue }
                let label = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if label == filename { foundSavedTitle = true }
                if label != filename && (label.hasPrefix(filename + " —") || label.hasPrefix(filename + " -")) {
                    fail("Pen has unsaved changes in the working copy. Save them in Pen and retry.")
                }
            }
        }
        if let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
            pending.append(contentsOf: children.reversed())
        }
    }
    guard foundSavedTitle else {
        fail("Could not confirm the Pen working copy is saved. Close its window manually and retry.")
    }
}

if CommandLine.arguments.dropFirst().elementsEqual(["status"]) {
    print(AXIsProcessTrusted() ? "authorized" : "not authorized")
    exit(0)
}

guard CommandLine.arguments.count == 3, ["close", "inspect"].contains(CommandLine.arguments[1]) else {
    fail("Usage: pen-reopen status | close <absolute .pen path> | inspect <absolute .pen path>")
}
let action = CommandLine.arguments[1]
let target = URL(fileURLWithPath: CommandLine.arguments[2]).standardizedFileURL.resolvingSymlinksInPath()
guard let running = NSRunningApplication.runningApplications(withBundleIdentifier: penBundleID).first else {
    if action == "inspect" { print("Pen is not running.") }
    exit(0)
}
guard AXIsProcessTrusted() else {
    fail("macOS Accessibility access is required to close the existing Pen working-copy window.")
}
let app = AXUIElementCreateApplication(running.processIdentifier)
AXUIElementSetMessagingTimeout(app, 0.5)
guard let available = windows(app) else { fail("Could not read Pen windows.") }
let selected = available.filter { matches($0, target) }
if action == "inspect" {
    print("Pen windows: \(available.count), matching working copy: \(selected.count)")
    for window in available {
        let attributes = documentAttributesForWindow(window).map { name, value in
            if let url = documentURL(value) {
                return "\(name)=\(url.path == target.path ? "target" : "other file")"
            }
            if let label = value as? String, label.contains(target.lastPathComponent) {
                return "\(name)=working-copy label"
            }
            return "\(name)=other value"
        }
        print(attributes.joined(separator: ", "))
    }
    exit(0)
}
if selected.isEmpty {
    fail("Could not identify an open Pen window for the exact working-copy path. Open the working copy in Pen and retry.")
}
guard selected.count == 1, let window = selected.first else {
    fail("More than one Pen window matches the working copy.")
}
verifySaved(window, filename: target.lastPathComponent)
guard let close = attribute(window, kAXCloseButtonAttribute),
      CFGetTypeID(close) == AXUIElementGetTypeID(),
      AXUIElementPerformAction(close as! AXUIElement, kAXPressAction as CFString) == .success else {
    fail("Could not close the Pen working-copy window.")
}
let deadline = ProcessInfo.processInfo.systemUptime + 3
repeat {
    if running.isTerminated { exit(0) }
    if let remaining = windows(app), !remaining.contains(where: { CFEqual($0, window) }) { exit(0) }
    Thread.sleep(forTimeInterval: 0.075)
} while ProcessInfo.processInfo.systemUptime < deadline
fail("The Pen working-copy window is still open. Check for an unsaved-changes dialog.")
