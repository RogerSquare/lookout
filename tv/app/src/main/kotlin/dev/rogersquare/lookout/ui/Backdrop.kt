package dev.rogersquare.lookout.ui
// Adapted from AirBridge (Documents/opencode/airbridge/app/.../ui) so both apps share one tvOS look.

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Shader
import android.os.SystemClock
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.runtime.mutableStateOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/** CSS `ease-in-out`. */
val EaseInOut = CubicBezierEasing(0.42f, 0f, 0.58f, 1f)

/**
 * Mockup backdrop: near-black with three 760-820 px radial blobs, blur(90px), opacity
 * .45/.38/.32, drifting over 26/32/38 s (alternate, ease-in-out), plus a faint top/bottom
 * darkening. Positions below are in dp of the 960x540 dp screen (mockup px * 0.75).
 *
 * Performance (weak MediaTek GPU at 1080p): drawing three screen-sized translucent gradient
 * layers per frame took ~105 ms a frame. A Gaussian-blurred radial fade has no detail, so the
 * whole backdrop is rendered on the CPU into a 320x180 bitmap (1/6 of the screen), off the main
 * thread, 4 times a second, and drawn upscaled with bilinear filtering: one textured quad on
 * the GPU, and nothing at all between updates.
 */
private class Blob(
    val x: Float, val y: Float, val size: Float, val color: Color, val opacity: Float,
    val dx: Float, val dy: Float, val s0: Float, val s1: Float, val periodMs: Long,
)

private val Blobs = listOf(
    Blob(-90f, -135f, 570f, Lk.Graphite[0], 0.45f, 165f, 90f, 1f, 1.15f, 26_000),
    Blob(495f, 45f, 615f, Lk.Graphite[1], 0.38f, -195f, -60f, 1.1f, 0.95f, 32_000),
    Blob(195f, 285f, 570f, Lk.Graphite[2], 0.32f, 90f, -120f, 0.95f, 1.12f, 38_000),
)

private const val SCREEN_W_DP = 960f
private const val BMP_W = 320
private const val BMP_H = 180
/** 4 updates/s: the drift is < 2 dp per update, invisible on a blurred blob. */
private const val FRAME_MS = 250L

/** Gaussian-like falloff of the blurred blob (stops at fractions of the visible radius). */
private val STOPS = floatArrayOf(0f, 0.2f, 0.4f, 0.6f, 0.8f, 1f)
private val LEVELS = floatArrayOf(0.85f, 0.76f, 0.55f, 0.30f, 0.10f, 0f)

private class BackdropRenderer {
    val bitmap: Bitmap = Bitmap.createBitmap(BMP_W, BMP_H, Bitmap.Config.ARGB_8888)
    val image = bitmap.asImageBitmap()
    private val canvas = Canvas(bitmap)
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val k = BMP_W / SCREEN_W_DP // bitmap px per dp
    private val shade = Paint().apply {
        shader = LinearGradient(
            0f, 0f, 0f, BMP_H.toFloat(),
            intArrayOf(Color.Black.copy(alpha = 0.2f).toArgb(), 0, Color.Black.copy(alpha = 0.3f).toArgb()),
            floatArrayOf(0f, 0.45f, 1f), Shader.TileMode.CLAMP,
        )
    }

    fun render(nowMs: Long) {
        canvas.drawColor(Lk.Bg.toArgb())
        for (b in Blobs) {
            // Alternate (ping-pong) with ease-in-out, like `animation: ... infinite alternate`.
            val phase = (nowMs % (2 * b.periodMs)).toFloat() / b.periodMs
            val v = EaseInOut.transform(if (phase > 1f) 2f - phase else phase)
            val scale = b.s0 + (b.s1 - b.s0) * v
            val cx = (b.x + b.size / 2 + b.dx * v) * k
            val cy = (b.y + b.size / 2 + b.dy * v) * k
            val r = b.size * 0.6f * scale * k // visible radius once the 90 px blur is applied
            val colors = IntArray(STOPS.size) { b.color.copy(alpha = LEVELS[it] * b.opacity).toArgb() }
            paint.shader = RadialGradient(cx, cy, r, colors, STOPS, Shader.TileMode.CLAMP)
            canvas.drawCircle(cx, cy, r, paint)
        }
        canvas.drawRect(0f, 0f, BMP_W.toFloat(), BMP_H.toFloat(), shade)
    }
}

@Composable
fun Backdrop(modifier: Modifier = Modifier) {
    // Two renderers (double buffer): one is drawn while the other is re-rendered on a background
    // thread, then they swap. The main thread only invalidates this node's draw ~4 times a second.
    val front = remember { mutableStateOf(BackdropRenderer().also { it.render(SystemClock.uptimeMillis()) }) }
    LaunchedEffect(Unit) {
        var back = BackdropRenderer()
        while (true) {
            delay(FRAME_MS)
            withContext(Dispatchers.Default) { back.render(SystemClock.uptimeMillis()) }
            val shown = front.value
            front.value = back
            back = shown
        }
    }
    Box(
        // Own RenderNode: a bitmap update re-records only this node, not the screen above it.
        modifier.fillMaxSize().graphicsLayer().drawBehind {
            drawImage(
                front.value.image, srcOffset = IntOffset.Zero, srcSize = IntSize(BMP_W, BMP_H),
                dstOffset = IntOffset.Zero, dstSize = IntSize(size.width.toInt(), size.height.toInt()),
                filterQuality = FilterQuality.Low,
            )
        },
    )
}
