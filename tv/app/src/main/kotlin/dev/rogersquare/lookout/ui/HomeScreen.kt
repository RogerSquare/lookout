package dev.rogersquare.lookout.ui

import android.graphics.BitmapFactory
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.tv.material3.Border
import androidx.tv.material3.ClickableSurfaceDefaults
import androidx.tv.material3.Glow
import androidx.tv.material3.LocalContentColor
import androidx.tv.material3.Surface
import dev.rogersquare.lookout.CameraInfo
import dev.rogersquare.lookout.CameraOverlay
import dev.rogersquare.lookout.R
import kotlinx.coroutines.delay

/** Everything the home screen shows. Updated from the hub's SSE stream by MainActivity. */
class HomeModel {
    var cameras by mutableStateOf(listOf<CameraInfo>())
    var selected by mutableStateOf<String?>(null)
    var connected by mutableStateOf(false)
    var alertsEnabled by mutableStateOf(true)
    var pauseOnAlert by mutableStateOf(false)
    var chime by mutableStateOf(true)
    var corner by mutableStateOf("bottom_right")
    var size by mutableStateOf(0.40f)

    val camera get() = cameras.firstOrNull { it.id == selected } ?: cameras.firstOrNull()
}

class HomeActions(
    val showCamera: (String) -> Unit,
    val toggleAlerts: () -> Unit,
    val togglePause: () -> Unit,
    val toggleChime: () -> Unit,
    /** Per-camera alerts, shared through the hub (also used by the desktop app). */
    val toggleCameraAlerts: (String) -> Unit,
    val nextCorner: () -> Unit,
    val nextSize: () -> Unit,
    val preview: () -> Unit,
    /** Fetches a hub path (e.g. a live JPEG); null on failure. */
    val fetch: suspend (String) -> ByteArray?,
)

/**
 * One minimal screen, tvOS-style: a large live view of the selected camera on the left (Select
 * shows it as the popup), camera pills under it, and two short settings groups on the right.
 */
@Composable
fun HomeScreen(m: HomeModel, a: HomeActions) {
    val first = remember { FocusRequester() }
    Box(Modifier.fillMaxSize().background(Lk.Bg)) {
        Backdrop()
        Row(
            Modifier.fillMaxSize().padding(start = 56.dp, end = 48.dp, top = 44.dp, bottom = 36.dp),
            horizontalArrangement = Arrangement.spacedBy(44.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1.12f)) {
                LiveCard(m, a, Modifier.focusRequester(first))
                Spacer(Modifier.height(18.dp))
                val cam = m.camera
                LkText(cam?.name ?: "Lookout", 30.sp, color = Color.White, weight = FontWeight.SemiBold, display = true)
                Spacer(Modifier.height(4.dp))
                StatusLine(cam, m.connected)
                if (m.cameras.size > 1) {
                    Spacer(Modifier.height(18.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        for (c in m.cameras) CameraPill(c, selected = c.id == cam?.id, onFocus = { m.selected = c.id }) { a.showCamera(c.id) }
                    }
                }
            }
            // Scrolls when focus moves past the bottom (focused rows are brought into view)
            Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(vertical = 12.dp)) {
                Section("Alerts")
                Group {
                    SettingRow("Person alerts", if (m.alertsEnabled) "On" else "Off", onClick = a.toggleAlerts)
                    SettingRow("Pause video during alerts", if (m.pauseOnAlert) "On" else "Off", onClick = a.togglePause)
                    SettingRow("Chime", if (m.chime) "On" else "Off", onClick = a.toggleChime)
                }
                if (m.cameras.isNotEmpty()) {
                    Spacer(Modifier.height(22.dp))
                    Section("Cameras")
                    Group {
                        for (c in m.cameras) {
                            SettingRow(c.name, if (c.alerts) "Alerts On" else "Alerts Off",
                                onClick = { a.toggleCameraAlerts(c.id) })
                        }
                    }
                }
                Spacer(Modifier.height(22.dp))
                Section("Popup")
                Group {
                    SettingRow("Position", cornerName(m.corner), chevron = true, onClick = a.nextCorner)
                    SettingRow("Size", sizeName(m.size), chevron = true, onClick = a.nextSize)
                    SettingRow("Preview", "Play", play = true, onClick = a.preview)
                }
                Spacer(Modifier.height(16.dp))
                LkText(
                    "On the popup: Up/Down resize · Left/Right move · Back closes",
                    11.sp, color = Lk.white(0.42f), modifier = Modifier.padding(start = 10.dp),
                )
            }
        }
    }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
}

@Composable
private fun LiveCard(m: HomeModel, a: HomeActions, modifier: Modifier) {
    val cam = m.camera
    var image by remember { mutableStateOf<ImageBitmap?>(null) }
    var focused by remember { mutableStateOf(false) }
    // While the popup shows this camera (e.g. Preview), blank the card: no second copy on screen
    val popupCamera by CameraOverlay.showing.collectAsState()
    val inPopup = popupCamera != null && popupCamera == cam?.id
    // A JPEG a second is plenty for a preview and far cheaper than a second video decoder.
    LaunchedEffect(cam?.id, inPopup) {
        if (inPopup) return@LaunchedEffect
        image = null
        val id = cam?.id ?: return@LaunchedEffect
        while (true) {
            a.fetch("/api/cameras/$id/live.jpg")?.let { bytes ->
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.let { image = it.asImageBitmap() }
            }
            delay(1_000)
        }
    }
    val shape = RoundedCornerShape(22.dp)
    Surface(
        onClick = { cam?.let { a.showCamera(it.id) } },
        modifier = modifier.fillMaxWidth().aspectRatio(16f / 9f).onFocusChanged { focused = it.isFocused },
        shape = ClickableSurfaceDefaults.shape(shape),
        colors = ClickableSurfaceDefaults.colors(
            containerColor = Lk.white(0.06f), focusedContainerColor = Lk.white(0.06f),
            pressedContainerColor = Lk.white(0.06f),
        ),
        scale = ClickableSurfaceDefaults.scale(focusedScale = 1.03f, pressedScale = 1.0f),
        border = ClickableSurfaceDefaults.border(
            border = Border(BorderStroke(0.75.dp, Lk.white(0.16f)), shape = shape),
            focusedBorder = Border(BorderStroke(3.dp, Color.White), shape = shape),
        ),
        glow = ClickableSurfaceDefaults.glow(glow = Glow.None, focusedGlow = Glow.None),
    ) {
        Box(Modifier.fillMaxSize()) {
            val shown = if (inPopup) null else image
            shown?.let { Image(it, null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop) }
                ?: LkText(
                    when {
                        inPopup -> "Showing popup preview"
                        m.connected -> "Loading…"
                        else -> "Connecting to hub…"
                    },
                    15.sp, color = Lk.white(0.5f), modifier = Modifier.align(Alignment.Center),
                )
            val hint by animateFloatAsState(if (focused) 1f else 0f, label = "hint")
            if (hint > 0f) {
                Row(
                    Modifier.align(Alignment.BottomStart).padding(14.dp)
                        .clip(Pill).background(Color.Black.copy(alpha = 0.55f * hint))
                        .padding(horizontal = 12.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Image(painterResource(R.drawable.play_fill), null, Modifier.size(11.dp),
                        colorFilter = ColorFilter.tint(Color.White.copy(alpha = hint)))
                    Spacer(Modifier.width(7.dp))
                    LkText("Show on screen", 13.sp, color = Color.White.copy(alpha = hint), weight = FontWeight.Medium)
                }
            }
        }
    }
}

@Composable
private fun StatusLine(cam: CameraInfo?, connected: Boolean) {
    val (text, alert) = when {
        !connected -> "Hub unreachable" to false
        cam == null -> "No cameras" to false
        !cam.connected -> "Camera offline" to false
        cam.person -> "Person at the door" to true
        else -> "No one at the door" to false
    }
    val dot by animateColorAsState(if (alert) Color(0xFFFF453A) else Lk.white(0.35f), label = "dot")
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(8.dp).clip(CircleShape).background(dot))
        Spacer(Modifier.width(8.dp))
        LkText(text, 15.sp, color = if (alert) Color.White else Lk.white(0.6f))
    }
}

@Composable
private fun CameraPill(c: CameraInfo, selected: Boolean, onFocus: () -> Unit, onClick: () -> Unit) {
    GlassClickable(
        onClick = onClick,
        modifier = Modifier.onFocusChanged { if (it.isFocused) onFocus() },
        shape = Pill,
        fill = if (selected) Lk.white(0.22f) else Lk.white(0.10f),
        borderColor = Lk.white(0.16f),
        focusedScale = 1.06f,
    ) {
        Row(Modifier.padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            if (c.person) {
                Box(Modifier.size(6.dp).clip(CircleShape).background(Color(0xFFFF453A)))
                Spacer(Modifier.width(7.dp))
            }
            LkText(c.name, 14.sp, weight = FontWeight.Medium)
        }
    }
}

@Composable
private fun Section(title: String) =
    LkText(title.uppercase(), 11.5.sp, color = Lk.white(0.5f), weight = FontWeight.SemiBold,
        tracking = 0.06.sp, modifier = Modifier.padding(start = 10.dp, bottom = 8.dp))

@Composable
private fun Group(content: @Composable () -> Unit) =
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) { content() }

/** tvOS settings row: glass when idle, solid white with dark text when focused. */
@Composable
private fun SettingRow(label: String, value: String, chevron: Boolean = false, play: Boolean = false, onClick: () -> Unit) {
    GlassClickable(
        onClick = onClick,
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        fill = Lk.white(0.08f),
        borderColor = null,
        focusedScale = 1.03f,
    ) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 13.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            LkText(label, 16.sp, modifier = Modifier.weight(1f))
            val tint = LocalContentColor.current.copy(alpha = 0.6f)
            if (play) {
                Image(painterResource(R.drawable.play_fill), null, Modifier.size(11.dp), colorFilter = ColorFilter.tint(tint))
                Spacer(Modifier.width(7.dp))
            }
            LkText(value, 16.sp, color = tint)
            if (chevron) {
                Spacer(Modifier.width(8.dp))
                Image(painterResource(R.drawable.chevron_right), null, Modifier.height(12.dp).width(7.dp),
                    colorFilter = ColorFilter.tint(tint))
            }
        }
    }
}

private fun cornerName(c: String) = c.split('_').joinToString(" ") { it.replaceFirstChar(Char::uppercase) }

private fun sizeName(f: Float) = when {
    f < 0.35f -> "Small"
    f < 0.45f -> "Medium"
    f < 0.55f -> "Large"
    else -> "Extra Large"
}
