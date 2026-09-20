// nudge — post REAL trusted input straight to Safari's process via CGEventPostToPid,
// so Apple's "你已有一段时间未进行任何操作" idle timer sees genuine interaction
// WITHOUT activating Safari (no focus steal, no window pop).
//
//   nudge            -> tiny relative mouse move to Safari
//   nudge key        -> Shift keydown/up to Safari
//
// Requires Accessibility permission for whatever runs it (your terminal).
// Build: swiftc -O nudge.swift -o nudge
import Cocoa

func safariPid() -> pid_t? {
    for app in NSWorkspace.shared.runningApplications {
        if app.bundleIdentifier == "com.apple.Safari" { return app.processIdentifier }
    }
    return nil
}

let pid = safariPid() ?? 0
guard pid != 0 else { print("NO_SAFARI"); exit(3) }

let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "move"

if mode == "key" {
    let src = CGEventSource(stateID: .hidSystemState)
    let down = CGEvent(keyboardEventSource: src, virtualKey: 56, keyDown: true)   // Shift
    let up = CGEvent(keyboardEventSource: src, virtualKey: 56, keyDown: false)
    down?.postToPid(pid)
    up?.postToPid(pid)
    print("OK key \(pid)")
} else {
    // A 1px move out and back — a genuine mouse event, no visible cursor change.
    if let cur = CGEvent(source: nil)?.location {
        let dst = CGPoint(x: cur.x + 1, y: cur.y + 1)
        for to in [dst, cur] {
            let e = CGEvent(mouseEventSource: CGEventSource(stateID: .hidSystemState),
                            mouseType: .mouseMoved, mouseCursorPosition: to, mouseButton: .left)
            e?.postToPid(pid)
        }
    }
    print("OK move \(pid)")
}
