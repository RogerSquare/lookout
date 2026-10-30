package dev.rogersquare.lookout

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.os.IBinder
import android.util.Log
import android.view.KeyEvent
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Always-on listener: keeps the hub's alert stream open and shows [CameraOverlay] when someone is
 * detected. A foreground service (type specialUse) so the low-memory killer leaves it alone; it
 * starts at boot ([BootReceiver]) and when the app is opened.
 *
 * Intents: [ACTION_SHOW] (+ [EXTRA_CAMERA]) opens a camera by hand and keeps it up (pinned),
 * [ACTION_TOGGLE] does the same or closes it, [ACTION_TEST] fakes a detection on the first camera.
 */
class LookoutService : Service(), CameraOverlay.Listener {
    private lateinit var hub: HubClient
    private lateinit var overlay: CameraOverlay
    private val chime by lazy { Chime() }
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private val log by lazy { EventLog(File(getExternalFilesDir(null) ?: filesDir, "events.log")) }

    /** Latest camera list from the hub's `state` event. */
    private var cameras: Map<String, CameraInfo> = emptyMap()
    /** Cameras the user dismissed while someone was there: no re-pop until that person leaves. */
    private val dismissed = mutableSetOf<String>()

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, notification("Starting…"), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        hub = HubClient(BuildConfig.HUB_URL, ::onHubEvent, ::onHubConnected)
        overlay = CameraOverlay(this, hub, this)
        hub.start()
        log.write("service started, hub ${BuildConfig.HUB_URL}")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SHOW -> intent.getStringExtra(EXTRA_CAMERA)?.let { showByHand(it) }
            ACTION_TOGGLE -> intent.getStringExtra(EXTRA_CAMERA)?.let { id ->
                if (overlay.cameraId == id) onDismissed(id) else showByHand(id)
            }
            ACTION_TEST -> cameras.values.firstOrNull()?.let { cam ->
                log.write("test alert on ${cam.id}")
                // Preview always pops up, even from the home screen
                personDetected(HubEvent.PersonDetected(cam.id, cam.name, 1.0, cam.streams, "/api/cameras/${cam.id}/live.jpg"), preview = true)
                // No real person, so the hub will never send person_gone: hide it like one would
                overlay.setPerson(true)
                main.postDelayed({
                    if (overlay.cameraId == cam.id && !overlay.pinned && cameras[cam.id]?.person != true) overlay.hide()
                }, TEST_HIDE_MS)
                cameras[cam.id]?.let { cameras = cameras + (cam.id to it.copy(person = false)) }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        hub.stop()
        overlay.hide(animate = false)
        log.write("service stopped")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun showByHand(id: String) {
        val cam = cameras[id] ?: return
        log.write("opened $id by hand")
        overlay.show(cam.id, cam.name, cam.overlayStream, "/api/cameras/${cam.id}/live.jpg", cam.person, pinned = true)
    }

    private fun onHubConnected(connected: Boolean) {
        if (!connected) {
            updateNotification("Hub unreachable, retrying…")
            log.write("hub disconnected")
        }
    }

    private fun onHubEvent(event: HubEvent) {
        when (event) {
            is HubEvent.State -> {
                cameras = event.cameras.associateBy { it.id }
                log.write("state: alerts=${event.alertsEnabled} cameras=${event.cameras.joinToString { "${it.id}(${if (it.connected) "up" else "down"})" }}")
                updateNotification("Watching ${cameras.size} cameras" + if (event.alertsEnabled) "" else " (alerts off)")
                // Connected while someone is already at the door: show it, without the chime
                event.cameras.firstOrNull { it.person && it.id !in dismissed }?.let { cam ->
                    if (!overlay.isShowing && !MainActivity.inFront) overlay.show(cam.id, cam.name, cam.overlayStream,
                        "/api/cameras/${cam.id}/snapshot.jpg", personHere = true, pinned = false)
                }
            }
            is HubEvent.PersonDetected -> personDetected(event, preview = false)
            is HubEvent.PersonGone -> {
                cameras[event.camera]?.let { cameras = cameras + (event.camera to it.copy(person = false)) }
                log.write("person gone from ${event.camera} (${event.reason})")
                dismissed -= event.camera
                if (overlay.cameraId == event.camera) {
                    if (overlay.pinned) overlay.setPerson(false) else overlay.hide()
                }
            }
            is HubEvent.Alerts -> log.write("alerts ${if (event.enabled) "on" else "off"} ${event.cameras}")
            is HubEvent.CameraStatus -> {
                cameras[event.camera]?.let { cameras = cameras + (event.camera to it.copy(connected = event.connected)) }
                log.write("${event.camera} ${if (event.connected) "online" else "offline"}")
            }
        }
    }

    private fun personDetected(event: HubEvent.PersonDetected, preview: Boolean) {
        cameras[event.camera]?.let { cameras = cameras + (event.camera to it.copy(person = true)) }
        log.write("person at ${event.camera} (${"%.2f".format(Locale.US, event.confidence)})")
        if (event.camera in dismissed) return
        if (MainActivity.inFront && !preview) {
            // The home screen's live card already shows the camera: chime only, no popup over it
            if (chime(this)) chime.play()
            log.write("home screen open: chime only")
            return
        }
        val wasShowing = overlay.cameraId == event.camera
        val stream = event.streams["sd"] ?: event.streams["hd"]
        overlay.show(event.camera, event.name, stream, event.snapshot, personHere = true, pinned = false)
        if (!wasShowing) {
            pauseMedia()  // first: the chime itself would count as "something playing"
            if (chime(this)) chime.play()
        }
    }

    /** Whatever is playing paused for an alert (setting "Pause video during alerts"); resumed on hide. */
    private var pausedPlayback = false

    private fun pauseMedia() {
        if (!pauseOnAlert(this) || pausedPlayback) return
        val audio = getSystemService(AudioManager::class.java)
        if (!audio.isMusicActive) return  // nothing playing: nothing to pause or resume later
        sendMediaKey(KeyEvent.KEYCODE_MEDIA_PAUSE)
        pausedPlayback = true
        log.write("paused playback for the alert")
    }

    override fun onHidden() {
        if (!pausedPlayback) return
        pausedPlayback = false
        sendMediaKey(KeyEvent.KEYCODE_MEDIA_PLAY)
        log.write("resumed playback")
    }

    /** Same as the remote's media keys: goes to whichever app owns the active media session. */
    private fun sendMediaKey(code: Int) {
        val audio = getSystemService(AudioManager::class.java)
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
    }

    override fun onDismissed(cameraId: String) {
        if (cameras[cameraId]?.person == true) dismissed += cameraId
        overlay.hide()
        log.write("dismissed $cameraId")
    }

    private fun notification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Lookout", NotificationManager.IMPORTANCE_LOW))
        }
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("Lookout")
            .setContentText(text)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) =
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))

    /** Small append-only log at Android/data/<pkg>/files/events.log (pull with adb). */
    private class EventLog(private val file: File) {
        private val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US)
        fun write(line: String) {
            Log.i(TAG, line)
            runCatching {
                if (file.length() > 512 * 1024) file.renameTo(File(file.parentFile, "events.old.log"))
                file.appendText("${fmt.format(Date())} $line\n")
            }
        }
    }

    companion object {
        private const val TAG = "Lookout"
        private const val CHANNEL = "lookout"
        private const val NOTIFICATION_ID = 1
        const val ACTION_SHOW = "dev.rogersquare.lookout.SHOW"
        const val ACTION_TOGGLE = "dev.rogersquare.lookout.TOGGLE"
        const val ACTION_TEST = "dev.rogersquare.lookout.TEST"
        const val EXTRA_CAMERA = "camera"
        private const val TEST_HIDE_MS = 15_000L

        private const val KEY_PAUSE = "pause_on_alert"
        private const val KEY_CHIME = "chime"

        fun chime(context: Context) =
            context.getSharedPreferences("lookout", Context.MODE_PRIVATE).getBoolean(KEY_CHIME, true)

        fun setChime(context: Context, on: Boolean) =
            context.getSharedPreferences("lookout", Context.MODE_PRIVATE).edit().putBoolean(KEY_CHIME, on).apply()

        fun pauseOnAlert(context: Context) =
            context.getSharedPreferences("lookout", Context.MODE_PRIVATE).getBoolean(KEY_PAUSE, false)

        fun setPauseOnAlert(context: Context, on: Boolean) =
            context.getSharedPreferences("lookout", Context.MODE_PRIVATE).edit().putBoolean(KEY_PAUSE, on).apply()

        fun start(context: Context, action: String? = null, camera: String? = null) {
            val intent = Intent(context, LookoutService::class.java).setAction(action)
            camera?.let { intent.putExtra(EXTRA_CAMERA, it) }
            context.startForegroundService(intent)
        }
    }
}
