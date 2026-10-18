import AppKit
import ScreenCaptureKit
import CoreMedia
import ImageIO
import UniformTypeIdentifiers

// This worker sends only JPEG frames on stdout. It does not record audio or accept input.
func emit(_ status: String, _ image: Data? = nil) {
    let payload: [String: Any] = ["status": status,
        "image": image.map { $0.base64EncodedString() } as Any? ?? NSNull(),
        "captured_at": image == nil ? NSNull() : Date().timeIntervalSince1970]
    if let data = try? JSONSerialization.data(withJSONObject: payload) {
        FileHandle.standardOutput.write(data + Data([10]))
    }
}

@main @MainActor struct FleetWorker {
    static func main() async {
        if CommandLine.arguments.contains("--authorize") {
            _ = NSApplication.shared
            let granted = CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess()
            let alert = NSAlert()
            alert.messageText = granted ? "Desktop viewing is ready" : "Allow Fleet Worker to share the desktop"
            alert.informativeText = granted ? "Fleet captures only while a screen task is active and its desktop view is open." : "Open System Settings → Privacy & Security → Screen & System Audio Recording. Enable Fleet Worker, then open the desktop view in Fleet again. No audio is captured."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        guard CommandLine.arguments.contains("--capture") else { return }
        guard CGPreflightScreenCaptureAccess() else { emit("permission_required"); return }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) else { emit("unavailable"); return }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.width = min(1280, display.width)
            config.height = max(1, display.height * config.width / display.width)
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.queueDepth = 2
            config.showsCursor = true
            config.capturesAudio = false
            while true {
                let session = CGSessionCopyCurrentDictionary() as? [String: Any]
                if session?["CGSSessionScreenIsLocked"] as? Bool == true {
                    emit("locked")
                } else {
                    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
                    let data = NSMutableData()
                    if let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) {
                        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.6] as CFDictionary)
                        if CGImageDestinationFinalize(destination), data.length <= 1024 * 1024 { emit("live", data as Data) }
                    }
                }
                try await Task.sleep(nanoseconds: 1_000_000_000)
            }
        } catch { emit("unavailable") }
    }
}
