package dev.rogersquare.lookout.ui
// Adapted from AirBridge (Documents/opencode/airbridge/app/.../ui) so both apps share one tvOS look.

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import androidx.tv.material3.Border
import androidx.tv.material3.ClickableSurfaceDefaults
import androidx.tv.material3.Glow
import androidx.tv.material3.LocalContentColor
import androidx.tv.material3.Surface

/*
 * Mockup (1280x720 px) -> Android TV (960x540 dp): every size is px * 0.75.
 * Fonts: SF Pro (Apple; private personal use only, see NOTICE), subset to Latin.
 */

object Lk {
    val Bg = Color(0xFF0B0B0C)
    val Ink = Color(0xFF0B0B0F)
    val Symbol = Color(0xFFF5F5F7)
    val Graphite = listOf(Color(0xFF2E2E33), Color(0xFF232327), Color(0xFF34322F))

    fun white(a: Float) = Color.White.copy(alpha = a)

    // The TV's own system font (Roboto / Google Sans); the weights carry the hierarchy
    val Display = FontFamily.SansSerif
    val Text = FontFamily.SansSerif

    /** Glass fill + hairline border with a brighter top edge (the mockup's inset highlight). */
    fun glassBorder(base: Float, top: Float) = Brush.verticalGradient(
        0f to white(top), 0.06f to white(base), 1f to white(base * 0.8f),
    )
}

@Composable
fun LkText(
    text: String,
    size: TextUnit,
    modifier: Modifier = Modifier,
    color: Color = LocalContentColor.current,
    weight: FontWeight = FontWeight.Normal,
    display: Boolean = false,
    tracking: TextUnit = if (display) (-0.02).em else (-0.01).em,
    align: TextAlign = TextAlign.Start,
    lineHeight: TextUnit = TextUnit.Unspecified,
    maxLines: Int = 1,
) {
    BasicText(
        text = text,
        modifier = modifier,
        maxLines = maxLines,
        overflow = TextOverflow.Ellipsis,
        style = TextStyle(
            color = color,
            fontSize = size,
            fontWeight = weight,
            fontFamily = if (display) Lk.Display else Lk.Text,
            letterSpacing = tracking,
            textAlign = align,
            lineHeight = lineHeight,
        ),
    )
}

/**
 * tvOS-style focusable control on androidx.tv Surface: unfocused = translucent glass with white
 * content; focused = solid white with black content, slightly scaled, with a soft shadow.
 */
@Composable
fun GlassClickable(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    shape: Shape = CircleShape,
    enabled: Boolean = true,
    fill: Color = Lk.white(0.14f),
    borderColor: Color? = Lk.white(0.22f),
    contentColor: Color = Color.White,
    focusedFill: Color = Color.White,
    focusedContent: Color = Lk.Ink,
    focusedScale: Float = 1.05f,
    glow: Boolean = true,
    content: @Composable BoxScope.() -> Unit,
) {
    Surface(
        onClick = onClick,
        modifier = modifier,
        enabled = enabled,
        shape = ClickableSurfaceDefaults.shape(shape),
        colors = ClickableSurfaceDefaults.colors(
            containerColor = fill,
            contentColor = contentColor,
            focusedContainerColor = focusedFill,
            focusedContentColor = focusedContent,
            pressedContainerColor = Lk.white(0.85f),
            pressedContentColor = focusedContent,
            disabledContainerColor = fill,
            disabledContentColor = contentColor,
        ),
        scale = ClickableSurfaceDefaults.scale(focusedScale = focusedScale, pressedScale = focusedScale * 0.97f),
        border = ClickableSurfaceDefaults.border(
            border = if (borderColor != null) Border(BorderStroke(0.75.dp, borderColor), shape = shape) else Border.None,
            focusedBorder = Border.None,
            pressedBorder = Border.None,
        ),
        glow = ClickableSurfaceDefaults.glow(
            glow = Glow.None,
            // No elevation glow: shadows were the GPU bottleneck on this SoC (24 ms/frame issue+GPU).
            focusedGlow = Glow.None,
        ),
        content = content,
    )
}

val Pill = RoundedCornerShape(percent = 50)

/**
 * Cheap drop shadow: three translucent rounded rects drawn behind, slightly larger and lower.
 * Replaces Modifier.shadow, whose elevation shadows cost most of the GPU frame time on the Nebula.
 */
fun Modifier.softShadow(corner: androidx.compose.ui.unit.Dp): Modifier = this.drawBehind {
    val r = corner.toPx()
    for ((grow, alpha) in SHADOW_STEPS) {
        val g = grow.dp.toPx()
        drawRoundRect(
            Color.Black.copy(alpha = alpha),
            topLeft = androidx.compose.ui.geometry.Offset(-g, -g + g * 0.6f),
            size = androidx.compose.ui.geometry.Size(size.width + 2 * g, size.height + 2 * g),
            cornerRadius = androidx.compose.ui.geometry.CornerRadius(r + g),
        )
    }
}

private val SHADOW_STEPS = listOf(4f to 0.16f, 9f to 0.10f, 15f to 0.06f)
