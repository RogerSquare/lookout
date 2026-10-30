package dev.rogersquare.lookout

import android.os.Handler
import android.os.Looper
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Keeps one SSE connection to the hub open, reconnecting with backoff (1 s .. 30 s) after Wi-Fi
 * drops, hub restarts or a silent dead socket (the hub sends a keepalive every 15 s, so a 40 s
 * read timeout catches it). Callbacks run on the main thread.
 */
class HubClient(
    val baseUrl: String,
    private val onEvent: (HubEvent) -> Unit,
    private val onConnected: (Boolean) -> Unit,
) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(40, TimeUnit.SECONDS)
        .build()
    private val main = Handler(Looper.getMainLooper())
    private var source: EventSource? = null
    private var running = false
    private var backoffMs = INITIAL_BACKOFF_MS
    private val reconnect = Runnable { if (running) connect() }

    fun start() {
        if (running) return
        running = true
        connect()
    }

    fun stop() {
        running = false
        main.removeCallbacks(reconnect)
        source?.cancel()
        source = null
    }

    private fun connect() {
        val request = Request.Builder().url("$baseUrl/events").header("Accept", "text/event-stream").build()
        source = EventSources.createFactory(http).newEventSource(request, object : EventSourceListener() {
            override fun onOpen(eventSource: EventSource, response: Response) {
                main.post { backoffMs = INITIAL_BACKOFF_MS; onConnected(true) }
            }

            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                val event = HubEvent.parse(type ?: return, data) ?: return
                main.post { onEvent(event) }
            }

            override fun onClosed(eventSource: EventSource) = scheduleRetry()

            override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) = scheduleRetry()
        })
    }

    private fun scheduleRetry() {
        main.post {
            onConnected(false)
            if (!running) return@post
            main.removeCallbacks(reconnect)
            main.postDelayed(reconnect, backoffMs)
            backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
        }
    }

    /** Shared alerts switch: POST /api/alerts. [camera] null = all cameras. */
    fun setAlerts(enabled: Boolean, camera: String? = null, done: (Boolean) -> Unit = {}) {
        val body = JSONObject().put("enabled", enabled).apply { camera?.let { put("camera", it) } }
        val request = Request.Builder().url("$baseUrl/api/alerts")
            .post(body.toString().toRequestBody("application/json".toMediaType())).build()
        http.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { main.post { done(false) } }
            override fun onResponse(call: Call, response: Response) {
                val ok = response.use { it.isSuccessful }
                main.post { done(ok) }
            }
        })
    }

    /** Fetches a snapshot JPEG (e.g. the frame that raised an alert) off the main thread. */
    fun fetch(path: String, done: (ByteArray?) -> Unit) {
        val url = if (path.startsWith("http")) path else "$baseUrl$path"
        http.newCall(Request.Builder().url(url).build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { main.post { done(null) } }
            override fun onResponse(call: Call, response: Response) {
                val bytes = response.use { if (it.isSuccessful) it.body?.bytes() else null }
                main.post { done(bytes) }
            }
        })
    }

    companion object {
        const val INITIAL_BACKOFF_MS = 1_000L
        const val MAX_BACKOFF_MS = 30_000L
    }
}
