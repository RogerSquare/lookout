import Foundation

/// Reads the hub's multipart JPEG stream (live.mjpeg) on one connection and hands back the newest
/// frame. Parts are framed by their Content-Length, so the JPEG bytes are never scanned. When
/// several frames arrive in one chunk only the last one is delivered: the picture stays current
/// on a slow link instead of playing catch-up.
final class MJPEGStream: NSObject, URLSessionDataDelegate {
    private let onFrame: (Data) -> Void
    private let onEnd: (Error?) -> Void
    private var buffer = Data()
    private var session: URLSession?
    private let headerEnd = Data("\r\n\r\n".utf8)

    init(onFrame: @escaping (Data) -> Void, onEnd: @escaping (Error?) -> Void) {
        self.onFrame = onFrame
        self.onEnd = onEnd
    }

    func start(_ url: URL) {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 8        // no bytes for 8 s: drop it and reconnect
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        session = URLSession(configuration: config, delegate: self, delegateQueue: queue)
        session?.dataTask(with: url).resume()
    }

    func stop() {
        session?.invalidateAndCancel()
        session = nil
    }

    // URLSession understands multipart/x-mixed-replace itself: it starts a new response for every
    // part (image/jpeg, Content-Length as expectedContentLength) and passes only the part's body.
    private var partMode = false
    private var expected: Int64 = -1

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        if response.mimeType == "image/jpeg" {
            partMode = true
            expected = response.expectedContentLength
            buffer.removeAll(keepingCapacity: true)
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        if partMode {
            buffer.append(data)
            if expected > 0, buffer.count >= Int(expected) {
                onFrame(buffer.prefix(Int(expected)))
                buffer.removeAll(keepingCapacity: true)
            }
            return
        }
        // Raw stream (no part splitting): frame the parts by their Content-Length headers
        buffer.append(data)
        var newest: Data?
        while let head = buffer.range(of: headerEnd) {
            let header = String(decoding: buffer[buffer.startIndex..<head.lowerBound], as: UTF8.self)
            guard let length = Self.contentLength(header) else {
                // Not a part header (e.g. a stray boundary line): skip past it
                buffer = buffer.subdata(in: head.upperBound..<buffer.endIndex)
                continue
            }
            let start = head.upperBound
            guard buffer.distance(from: start, to: buffer.endIndex) >= length else { break }
            let end = buffer.index(start, offsetBy: length)
            newest = buffer.subdata(in: start..<end)
            buffer = buffer.subdata(in: end..<buffer.endIndex)
        }
        if buffer.count > 4_000_000 { buffer.removeAll() }   // garbage guard
        if let frame = newest { onFrame(frame) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        onEnd(error)
    }

    private static func contentLength(_ header: String) -> Int? {
        for line in header.split(whereSeparator: { $0 == "\r" || $0 == "\n" }) {
            let parts = line.split(separator: ":", maxSplits: 1)
            if parts.count == 2, parts[0].trimmingCharacters(in: .whitespaces).lowercased() == "content-length" {
                return Int(parts[1].trimmingCharacters(in: .whitespaces))
            }
        }
        return nil
    }
}
