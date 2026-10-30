package dev.rogersquare.lookout

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import dev.rogersquare.lookout.ui.HomeActions
import dev.rogersquare.lookout.ui.HomeModel
import dev.rogersquare.lookout.ui.HomeScreen
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Launcher entry: starts the always-on [LookoutService] and shows the home screen, which follows
 * the hub's alert stream live (cameras, person at the door, the shared alerts switch).
 */
class MainActivity : ComponentActivity() {
    private val model = HomeModel()
    private lateinit var hub: HubClient

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        LookoutService.start(this)
        hub = HubClient(BuildConfig.HUB_URL, ::onHubEvent) { model.connected = it }
        loadSettings()
        val actions = HomeActions(
            showCamera = { LookoutService.start(this, LookoutService.ACTION_SHOW, it) },
            toggleAlerts = {
                val next = !model.alertsEnabled
                model.alertsEnabled = next  // optimistic; the hub's `alerts` event confirms it
                hub.setAlerts(next) { ok -> if (!ok) model.alertsEnabled = !next }
            },
            togglePause = {
                LookoutService.setPauseOnAlert(this, !LookoutService.pauseOnAlert(this))
                loadSettings()
            },
            toggleChime = {
                LookoutService.setChime(this, !LookoutService.chime(this))
                loadSettings()
            },
            toggleCameraAlerts = { id ->
                val cam = model.cameras.firstOrNull { it.id == id } ?: return@HomeActions
                val next = !cam.alerts
                update(id) { it.copy(alerts = next) }  // optimistic; the hub's `alerts` event confirms it
                hub.setAlerts(next, id) { ok -> if (!ok) update(id) { it.copy(alerts = !next) } }
            },
            nextCorner = {
                val all = CameraOverlay.CORNERS
                CameraOverlay.setCorner(this, all[(all.indexOf(CameraOverlay.corner(this)) + 1) % all.size])
                loadSettings()
            },
            nextSize = {
                val next = SIZES.firstOrNull { it > CameraOverlay.sizeFraction(this) + 0.001f } ?: SIZES.first()
                CameraOverlay.setSizeFraction(this, next)
                loadSettings()
            },
            preview = { LookoutService.start(this, LookoutService.ACTION_TEST) },
            fetch = { path -> suspendCancellableCoroutine { c -> hub.fetch(path) { c.resume(it) } } },
        )
        setContent { HomeScreen(model, actions) }
    }

    override fun onStart() {
        super.onStart()
        loadSettings()
        hub.start()
    }

    override fun onStop() {
        hub.stop()
        super.onStop()
    }

    override fun onResume() {
        super.onResume()
        inFront = true
    }

    override fun onPause() {
        inFront = false
        super.onPause()
    }

    private fun loadSettings() {
        model.pauseOnAlert = LookoutService.pauseOnAlert(this)
        model.chime = LookoutService.chime(this)
        model.corner = CameraOverlay.corner(this)
        model.size = CameraOverlay.sizeFraction(this)
    }

    private fun onHubEvent(e: HubEvent) {
        when (e) {
            is HubEvent.State -> {
                model.cameras = e.cameras
                model.alertsEnabled = e.alertsEnabled
                if (model.selected == null) model.selected = e.cameras.firstOrNull()?.id
            }
            is HubEvent.PersonDetected -> update(e.camera) { it.copy(person = true) }
            is HubEvent.PersonGone -> update(e.camera) { it.copy(person = false) }
            is HubEvent.Alerts -> {
                model.alertsEnabled = e.enabled
                model.cameras = model.cameras.map { c -> e.cameras[c.id]?.let { c.copy(alerts = it) } ?: c }
            }
            is HubEvent.CameraStatus -> update(e.camera) { it.copy(connected = e.connected) }
        }
    }

    private fun update(id: String, change: (CameraInfo) -> CameraInfo) {
        model.cameras = model.cameras.map { if (it.id == id) change(it) else it }
    }

    companion object {
        private val SIZES = listOf(0.30f, 0.40f, 0.50f, 0.60f)

        /** The home screen is showing: its live card already shows the camera, so alerts don't pop up. */
        @Volatile
        var inFront = false
            private set
    }
}
