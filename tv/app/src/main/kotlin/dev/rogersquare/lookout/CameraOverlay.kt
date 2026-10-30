package dev.rogersquare.lookout

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Outline
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.KeyEvent
import android.view.TextureView
import android.view.View
import android.view.ViewOutlineProvider
import android.view.WindowManager
import android.view.animation.DecelerateInterpolator
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.rtsp.RtspMediaSource
import kotlinx.coroutines.flow.MutableStateFlow
import kotlin.math.roundToInt

/**
 * The picture-in-picture popup over whatever is playing (Plex, SmartTube...): live camera video in
 * the bottom-right corner, Apple TV-style. A TYPE_APPLICATION_OVERLAY window (needs "Display over
 * other apps"), shown with a fade + slide and removed with a fade + shrink.
 *
 * While it is visible the window takes key focus, so the remote drives it: Up/Down resize it in 5%
 * steps, Left/Right slide it to the other side (both remembered), Select toggles a temporary
 * enlarged view, Back dismisses it. The app underneath keeps playing (an overlay window isn't an
 * activity, so it doesn't pause it); focus returns to it when the popup goes away.
 *
 * The hub's snapshot of the detection frame is shown at once while RTSP connects (~1-2 s), so the
 * popup is never an empty box. Main thread only.
 */
class CameraOverlay(private val context: Context, private val hub: HubClient, private val listener: Listener) {
    interface Listener {
        /** The user closed the popup with Back. */
        fun onDismissed(cameraId: String)

        /** The popup is gone (any reason). */
        fun onHidden() {}
    }

    private val wm = context.getSystemService(WindowManager::class.java)
    private val density = context.resources.displayMetrics.density
    private var root: FrameLayout? = null
    private var params: WindowManager.LayoutParams? = null
    private var video: TextureView? = null
    private var still: ImageView? = null
    private var title: TextView? = null
    private var status: TextView? = null
    private var dot: View? = null
    private var player: ExoPlayer? = null
    private var sizeAnim: ValueAnimator? = null
    private var enlarged = false

    /** Camera currently shown, or null when the popup is hidden. */
    var cameraId: String? = null
        private set

    /** Opened by hand (launcher / tray-style toggle): stays up until the user closes it. */
    var pinned = false
        private set

    val isShowing get() = root != null

    fun show(camera: String, name: String, streamUrl: String?, snapshotPath: String?, personHere: Boolean, pinned: Boolean) {
        if (!Settings.canDrawOverlays(context)) {
            Log.w(TAG, "no overlay permission; run: adb shell appops set ${context.packageName} SYSTEM_ALERT_WINDOW allow")
            return
        }
        if (cameraId == camera && root != null) {
            this.pinned = this.pinned || pinned
            setPerson(personHere)
            return
        }
        hide(animate = false)
        cameraId = camera
        this.pinned = pinned
        enlarged = false
        buildWindow()
        title?.text = name
        setPerson(personHere)
        snapshotPath?.takeIf { it.isNotEmpty() }?.let { path ->
            hub.fetch(path) { bytes ->
                if (cameraId != camera || bytes == null) return@fetch
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.let { still?.setImageBitmap(it) }
            }
        }
        streamUrl?.let { play(it) }
        showing.value = camera
        root?.apply {
            alpha = 0f
            translationY = (if (corner(context).startsWith("top")) -24 else 24) * density  // slide in from the edge
            animate().alpha(1f).translationY(0f).setDuration(SHOW_MS).setInterpolator(DecelerateInterpolator(2f)).start()
        }
    }

    /** Left/Right on the remote: slide the popup to the other side, keeping top/bottom. */
    private fun moveSide(toRight: Boolean) {
        val v = root ?: return
        val lp = params ?: return
        val now = corner(context)
        val next = now.substringBefore('_') + if (toRight) "_right" else "_left"
        if (next == now) return
        setCorner(context, next)
        val shift = (if (toRight) 48 else -48) * density
        v.animate().cancel()
        v.animate().alpha(0f).translationX(shift).setDuration(140).withEndAction {
            lp.gravity = gravityFor(next)
            runCatching { wm.updateViewLayout(v, lp) }
            v.translationX = -shift
            v.animate().alpha(1f).translationX(0f).setDuration(SHOW_MS).setInterpolator(DecelerateInterpolator(2f)).start()
        }.start()
    }

    /** Updates the status line; the red dot means someone is in view right now. */
    fun setPerson(here: Boolean) {
        personHere = here
        status?.text = if (here) "Person detected" else "Live"
        dot?.background = circle(if (here) Color.rgb(255, 69, 58) else Color.argb(150, 255, 255, 255))
    }

    fun hide(animate: Boolean = true) {
        val v = root ?: return
        val done = {
            sizeAnim?.cancel()
            player?.release()
            player = null
            runCatching { wm.removeView(v) }
            if (root === v) {
                root = null; params = null; video = null; still = null; title = null; status = null; dot = null
                cameraId = null; pinned = false
                showing.value = null
                listener.onHidden()
            }
        }
        if (!animate) {
            v.animate().cancel()
            done()
            return
        }
        v.animate().cancel()
        v.animate().alpha(0f).scaleX(0.94f).scaleY(0.94f).setDuration(HIDE_MS).withEndAction(done).start()
    }

    private fun buildWindow() {
        val (w, h) = size(enlarged)
        val frame = object : FrameLayout(context) {
            override fun dispatchKeyEvent(event: KeyEvent): Boolean {
                if (event.action != KeyEvent.ACTION_UP) {
                    return event.keyCode in HANDLED_KEYS || super.dispatchKeyEvent(event)
                }
                when (event.keyCode) {
                    KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_ESCAPE -> {
                        cameraId?.let { listener.onDismissed(it) }
                        return true
                    }
                    KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER -> {
                        enlarged = !enlarged
                        animateToSize()
                        return true
                    }
                    KeyEvent.KEYCODE_DPAD_UP -> { stepSize(+1); return true }
                    KeyEvent.KEYCODE_DPAD_DOWN -> { stepSize(-1); return true }
                    KeyEvent.KEYCODE_DPAD_LEFT -> { moveSide(toRight = false); return true }
                    KeyEvent.KEYCODE_DPAD_RIGHT -> { moveSide(toRight = true); return true }
                }
                return super.dispatchKeyEvent(event)
            }
        }.apply {
            setBackgroundColor(Color.rgb(28, 28, 30))
            outlineProvider = object : ViewOutlineProvider() {
                override fun getOutline(view: View, outline: Outline) =
                    outline.setRoundRect(0, 0, view.width, view.height, RADIUS_DP * density)
            }
            clipToOutline = true
            isFocusable = true
            isFocusableInTouchMode = true
        }
        // TextureView, not SurfaceView: it is clipped to the rounded corners and fades with the window.
        val tv = TextureView(context)
        frame.addView(tv, FrameLayout.LayoutParams(-1, -1))
        val iv = ImageView(context).apply { scaleType = ImageView.ScaleType.CENTER_CROP }
        frame.addView(iv, FrameLayout.LayoutParams(-1, -1))
        frame.addView(buildPill(), FrameLayout.LayoutParams(-2, -2).apply {
            gravity = Gravity.TOP or Gravity.START
            setMargins((10 * density).toInt(), (10 * density).toInt(), 0, 0)
        })

        val lp = WindowManager.LayoutParams(
            w, h,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            // Focusable (no FLAG_NOT_FOCUSABLE) so Select/Back reach the popup while it's up.
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT,
        ).apply {
            // Anchored by its corner, so resizing grows away from the corner
            gravity = gravityFor(corner(context))
            x = (MARGIN_DP * density).toInt()
            y = (MARGIN_DP * density).toInt()
            title = "Lookout camera"
            windowAnimations = 0
        }
        runCatching { wm.addView(frame, lp) }.onFailure {
            Log.e(TAG, "addView failed", it)
            return
        }
        frame.requestFocus()
        root = frame; params = lp; video = tv; still = iv
    }

    private fun buildPill(): View {
        val d = density
        val name = TextView(context).apply {
            setTextColor(Color.WHITE); textSize = 13f; typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        }
        val sub = TextView(context).apply { setTextColor(Color.argb(210, 255, 255, 255)); textSize = 10.5f }
        val dotView = View(context)
        val text = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            addView(name); addView(sub)
        }
        title = name; status = sub; dot = dotView
        return LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding((10 * d).toInt(), (6 * d).toInt(), (12 * d).toInt(), (6 * d).toInt())
            background = GradientDrawable().apply {
                cornerRadius = 10 * d
                setColor(Color.argb(0xD9, 0x1C, 0x1C, 0x1E))
                setStroke((0.75f * d).toInt().coerceAtLeast(1), Color.argb(46, 255, 255, 255))
            }
            addView(dotView, LinearLayout.LayoutParams((8 * d).toInt(), (8 * d).toInt()).apply { marginEnd = (8 * d).toInt() })
            addView(text)
        }
    }

    private fun play(url: String) {
        val tv = video ?: return
        val p = ExoPlayer.Builder(context)
            // Live view: start as soon as possible, keep the buffer tiny.
            .setLoadControl(DefaultLoadControl.Builder().setBufferDurationsMs(500, 2_000, 250, 250).build())
            .build()
        p.trackSelectionParameters = p.trackSelectionParameters.buildUpon()
            .setTrackTypeDisabled(C.TRACK_TYPE_AUDIO, true).build()  // camera audio is never played
        p.volume = 0f
        p.setVideoTextureView(tv)
        p.addListener(object : Player.Listener {
            override fun onRenderedFirstFrame() {
                still?.animate()?.alpha(0f)?.setDuration(250)?.start()
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.w(TAG, "player error ${error.errorCodeName}; retrying", error)
                still?.alpha = 1f
                root?.postDelayed({ if (player === p) { p.prepare(); p.play() } }, 2_000)
            }
        })
        p.setMediaSource(RtspMediaSource.Factory().setForceUseRtpTcp(true).createMediaSource(MediaItem.fromUri(url)))
        p.playWhenReady = true
        p.prepare()
        player = p
    }

    /** Up/Down on the remote: resize in 5% steps; the size is remembered for every popup. */
    private fun stepSize(direction: Int) {
        val now = sizeFraction()
        val next = ((now + direction * SIZE_STEP) * 100).roundToInt() / 100f
        setSizeFraction(context, next)
        enlarged = false
        animateToSize()
        val pct = (sizeFraction() * 100).roundToInt()
        status?.text = "Size $pct%"
        root?.removeCallbacks(restoreStatus)
        root?.postDelayed(restoreStatus, 1_500)
    }

    private val restoreStatus = Runnable { setPerson(personHere) }
    private var personHere = false

    private fun sizeFraction() = sizeFraction(context)

    private fun animateToSize() {
        val v = root ?: return
        val lp = params ?: return
        val (w0, h0) = lp.width to lp.height
        val (w1, h1) = size(enlarged)
        sizeAnim?.cancel()
        sizeAnim = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = RESIZE_MS
            interpolator = DecelerateInterpolator(1.6f)
            addUpdateListener {
                val f = it.animatedValue as Float
                lp.width = (w0 + (w1 - w0) * f).toInt()
                lp.height = (h0 + (h1 - h0) * f).toInt()
                runCatching { wm.updateViewLayout(v, lp) }
            }
            start()
        }
    }

    /** Popup size in px, always 16:9: the chosen fraction of the screen width, ~1.6x when enlarged. */
    private fun size(large: Boolean): Pair<Int, Int> {
        val screenW = context.resources.displayMetrics.widthPixels
        val f = sizeFraction()
        val w = (screenW * if (large) (f * ENLARGE).coerceAtMost(MAX_ENLARGED) else f).toInt()
        return w to (w * 9 / 16)
    }

    private fun circle(color: Int) = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(color) }

    companion object {
        private const val TAG = "LookoutOverlay"
        private const val PREFS = "lookout"
        private const val KEY_SIZE = "popup_size"
        const val DEFAULT_SIZE = 0.40f   // fraction of the screen width
        const val MIN_SIZE = 0.20f
        const val MAX_SIZE = 0.70f
        const val SIZE_STEP = 0.05f
        const val ENLARGE = 1.55f
        const val MAX_ENLARGED = 0.90f

        /** Camera the popup is showing right now (null = hidden); the home screen watches it. */
        val showing = MutableStateFlow<String?>(null)

        private const val KEY_CORNER = "popup_corner"
        val CORNERS = listOf("bottom_right", "bottom_left", "top_left", "top_right")

        fun corner(context: Context): String =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_CORNER, null)
                ?.takeIf { it in CORNERS } ?: "bottom_right"

        fun setCorner(context: Context, corner: String) =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_CORNER, corner).apply()

        fun gravityFor(corner: String): Int =
            (if (corner.startsWith("top")) Gravity.TOP else Gravity.BOTTOM) or
                (if (corner.endsWith("left")) Gravity.START else Gravity.END)

        fun sizeFraction(context: Context): Float =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getFloat(KEY_SIZE, DEFAULT_SIZE)

        fun setSizeFraction(context: Context, fraction: Float) =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putFloat(KEY_SIZE, fraction.coerceIn(MIN_SIZE, MAX_SIZE)).apply()
        const val MARGIN_DP = 28f   // inside the projector's overscan-safe area
        const val RADIUS_DP = 14f
        const val SHOW_MS = 260L
        const val HIDE_MS = 200L
        const val RESIZE_MS = 280L
        private val HANDLED_KEYS = setOf(
            KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_ESCAPE,
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER,
            KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN,
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT,
        )
    }
}
