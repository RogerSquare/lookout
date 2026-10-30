package dev.rogersquare.lookout

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.sin

/**
 * Soft two-note "ding-dong" synthesised once at startup (no sound file to ship). It mixes over the
 * movie without taking audio focus. Played as media (not notification) audio: AirBridge only
 * captures media audio, so while the projector streams to the HomePod (speaker muted) the chime
 * goes to the HomePod too; a notification sound would be lost on the muted speaker.
 */
class Chime {
    private val pcm: ShortArray = synth()

    fun play() {
        val track = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build()
            )
            .setTransferMode(AudioTrack.MODE_STATIC)
            .setBufferSizeInBytes(pcm.size * 2)
            .build()
        track.write(pcm, 0, pcm.size)
        track.setNotificationMarkerPosition(pcm.size)
        track.setPlaybackPositionUpdateListener(object : AudioTrack.OnPlaybackPositionUpdateListener {
            override fun onMarkerReached(t: AudioTrack) = t.release()
            override fun onPeriodicNotification(t: AudioTrack) {}
        })
        track.play()
    }

    private fun synth(): ShortArray {
        val out = ShortArray((RATE * 1.3).toInt())
        // E6 then C6, each a sine plus a quiet octave partial with an exponential decay
        for ((startS, freq) in listOf(0.0 to 1318.5, 0.32 to 1046.5)) {
            val start = (startS * RATE).toInt()
            for (i in 0 until out.size - start) {
                val t = i.toDouble() / RATE
                val env = exp(-t * 4.5) * (1 - exp(-t * 400))  // 2.5 ms attack, soft tail
                val s = (sin(2 * PI * freq * t) + 0.25 * sin(4 * PI * freq * t)) * env * 0.22
                out[start + i] = (out[start + i] + s * Short.MAX_VALUE).toInt()
                    .coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort()
            }
        }
        return out
    }

    companion object {
        const val RATE = 44_100
    }
}
