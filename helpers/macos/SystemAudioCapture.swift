// Zero-admin meeting recorder — macOS system-audio helper (spec 8).
//
// Captures system audio with Apple's native ScreenCaptureKit and writes raw
// interleaved little-endian float32 frames to stdout.  No virtual audio
// driver is installed (no BlackHole, Loopback or Soundflower) and no
// elevation is requested: the only thing macOS asks for is the ordinary
// "Screen & System Audio Recording" privacy permission, which the user
// grants in System Settings.
//
// ScreenCaptureKit is a screen-capture API, so a display filter is required
// even though this program wants audio only.  Video is configured down to a
// minimal size and low frame rate, and video sample buffers are discarded.
//
// Protocol (see meetingcap/audio/macos.py):
//   meetingcap-system-audio --rate 48000 --channels 2
//   stdout : raw float32 frames, interleaved
//   stderr : diagnostics, one line each
//   exit   : non-zero if the stream cannot start (permission, no display, …)
//
// Build (no administrator password required):
//   swiftc -O -framework ScreenCaptureKit -framework AVFoundation \
//       -o meetingcap-system-audio SystemAudioCapture.swift

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - Arguments

struct Options {
    var sampleRate: Int = 48_000
    var channels: Int = 2

    static func parse(_ arguments: [String]) -> Options {
        var options = Options()
        var index = 1
        while index < arguments.count {
            switch arguments[index] {
            case "--rate":
                if index + 1 < arguments.count, let value = Int(arguments[index + 1]) {
                    options.sampleRate = value
                    index += 1
                }
            case "--channels":
                if index + 1 < arguments.count, let value = Int(arguments[index + 1]) {
                    options.channels = value
                    index += 1
                }
            default:
                break
            }
            index += 1
        }
        return options
    }
}

func log(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

func fail(_ message: String) -> Never {
    log(message)
    exit(1)
}

// MARK: - Capture

@available(macOS 13.0, *)
final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let options: Options
    private let output = FileHandle.standardOutput
    private let queue = DispatchQueue(label: "meetingcap.audio", qos: .userInitiated)
    private var stream: SCStream?

    init(options: Options) {
        self.options = options
    }

    func start() async throws {
        // A display is required to build a content filter, even for
        // audio-only capture.
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            fail("no display available for ScreenCaptureKit")
        }

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.sampleRate = options.sampleRate
        configuration.channelCount = options.channels
        // Do not record this process's own output: that is what keeps the
        // recorder from feeding its own audio back into the capture.
        configuration.excludesCurrentProcessAudio = true
        // Video is unavoidable but can be made nearly free.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await stream.startCapture()
        self.stream = stream
        log("capture started: \(options.sampleRate) Hz, \(options.channels) ch")
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }   // video is discarded
        guard let samples = interleavedFloat32(from: sampleBuffer) else { return }
        samples.withUnsafeBufferPointer { pointer in
            let data = Data(buffer: pointer)
            // A closed stdout means the Python side went away; exit quietly
            // rather than spinning on a broken pipe.
            do {
                try output.write(contentsOf: data)
            } catch {
                exit(0)
            }
        }
    }

    /// ScreenCaptureKit delivers non-interleaved float32; the Python side
    /// expects one interleaved block per callback.
    private func interleavedFloat32(from sampleBuffer: CMSampleBuffer) -> [Float]? {
        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer)
        guard status == noErr else { return nil }

        let buffers = UnsafeMutableAudioBufferListPointer(&audioBufferList)
        guard buffers.count > 0 else { return nil }

        let planes: [UnsafeBufferPointer<Float>] = buffers.compactMap { buffer in
            guard let data = buffer.mData else { return nil }
            let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
            return UnsafeBufferPointer(
                start: data.assumingMemoryBound(to: Float.self), count: count)
        }
        guard let first = planes.first, !first.isEmpty else { return nil }

        let frames = first.count
        let wanted = options.channels
        var interleaved = [Float](repeating: 0, count: frames * wanted)
        for channel in 0..<wanted {
            // Duplicate the last available plane when the stream provides
            // fewer channels than requested (mono source, stereo output).
            let plane = planes[min(channel, planes.count - 1)]
            for frame in 0..<frames where frame < plane.count {
                interleaved[frame * wanted + channel] = plane[frame]
            }
        }
        return interleaved
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("stream stopped: \(error.localizedDescription)")
    }
}

// MARK: - Entry point

if #available(macOS 13.0, *) {
    let options = Options.parse(CommandLine.arguments)
    let capture = SystemAudioCapture(options: options)
    let semaphore = DispatchSemaphore(value: 0)

    Task {
        do {
            try await capture.start()
        } catch {
            // A permission denial surfaces here; the Python side turns this
            // into an explanation, never a stack trace.
            log("permission or configuration error: \(error.localizedDescription)")
            exit(1)
        }
        semaphore.signal()
    }

    semaphore.wait()
    dispatchMain()
} else {
    fail("macOS 13 (Ventura) or newer is required for ScreenCaptureKit audio capture")
}
