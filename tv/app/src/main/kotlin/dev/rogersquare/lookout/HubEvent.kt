package dev.rogersquare.lookout

import org.json.JSONObject

/** One camera as the hub reports it (in the `state` event). */
data class CameraInfo(
    val id: String,
    val name: String,
    val connected: Boolean,
    val person: Boolean,
    val alerts: Boolean,
    val streams: Map<String, String>,
) {
    /** The overlay plays the 360p stream when there is one: the projector has ~1.7 GB of RAM. */
    val overlayStream: String? get() = streams["sd"] ?: streams["hd"]
}

/** Events from the Lookout Hub's SSE stream (`GET /events`). See hub/README.md. */
sealed interface HubEvent {
    data class State(val alertsEnabled: Boolean, val cameras: List<CameraInfo>) : HubEvent
    data class PersonDetected(
        val camera: String,
        val name: String,
        val confidence: Double,
        val streams: Map<String, String>,
        val snapshot: String,
    ) : HubEvent

    data class PersonGone(val camera: String, val reason: String) : HubEvent
    data class Alerts(val enabled: Boolean, val cameras: Map<String, Boolean>) : HubEvent
    data class CameraStatus(val camera: String, val connected: Boolean) : HubEvent

    companion object {
        /** Parses one SSE message; unknown types and malformed data return null. */
        fun parse(type: String, data: String): HubEvent? = runCatching {
            val o = JSONObject(data)
            when (type) {
                "state" -> {
                    val alerts = o.getJSONObject("alerts")
                    val cams = o.getJSONArray("cameras")
                    State(
                        alertsEnabled = alerts.getBoolean("enabled"),
                        cameras = (0 until cams.length()).map { i ->
                            val c = cams.getJSONObject(i)
                            CameraInfo(
                                id = c.getString("id"),
                                name = c.optString("name", c.getString("id")),
                                connected = c.optBoolean("connected"),
                                person = c.optBoolean("person"),
                                alerts = c.optBoolean("alerts", true),
                                streams = c.optJSONObject("streams").toStringMap(),
                            )
                        },
                    )
                }
                "person_detected" -> PersonDetected(
                    camera = o.getString("camera"),
                    name = o.optString("name", o.getString("camera")),
                    confidence = o.optDouble("confidence", 0.0),
                    streams = o.optJSONObject("streams").toStringMap(),
                    snapshot = o.optString("snapshot", ""),
                )
                "person_gone" -> PersonGone(o.getString("camera"), o.optString("reason", "left"))
                "alerts" -> Alerts(o.getBoolean("enabled"), o.optJSONObject("cameras").toBoolMap())
                "camera_status" -> CameraStatus(o.getString("camera"), o.getBoolean("connected"))
                else -> null
            }
        }.getOrNull()

        private fun JSONObject?.toStringMap(): Map<String, String> =
            this?.keys()?.asSequence()?.associateWith { getString(it) } ?: emptyMap()

        private fun JSONObject?.toBoolMap(): Map<String, Boolean> =
            this?.keys()?.asSequence()?.associateWith { getBoolean(it) } ?: emptyMap()
    }
}
