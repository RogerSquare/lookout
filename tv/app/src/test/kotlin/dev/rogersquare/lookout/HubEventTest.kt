package dev.rogersquare.lookout

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Payloads copied from the live hub (hub/hub.py) on 2026-10-27. */
class HubEventTest {
    @Test
    fun `state lists cameras and prefers the 360p stream for the overlay`() {
        val e = HubEvent.parse("state", """
            {"type": "state", "alerts": {"enabled": true, "cameras": {"kitchen": true, "bedroom": false}},
             "cameras": [
              {"id": "kitchen", "name": "Kitchen Cam", "connected": true, "person": false, "confidence": 0.0,
               "since": null, "alerts": true,
               "streams": {"hd": "rtsp://10.0.0.127:8555/kitchen", "sd": "rtsp://10.0.0.127:8555/kitchen_sd"},
               "snapshot": "/api/cameras/kitchen/snapshot.jpg"},
              {"id": "bedroom", "name": "Bedroom", "connected": false, "person": true, "confidence": 0.8,
               "since": "x", "alerts": false, "streams": {"hd": "rtsp://10.0.0.127:8555/bedroom"},
               "snapshot": "/api/cameras/bedroom/snapshot.jpg"}],
             "time": "2026-10-27T16:39:30.000+00:00"}
        """) as HubEvent.State
        assertTrue(e.alertsEnabled)
        assertEquals(listOf("kitchen", "bedroom"), e.cameras.map { it.id })
        assertEquals("rtsp://10.0.0.127:8555/kitchen_sd", e.cameras[0].overlayStream)
        assertEquals("rtsp://10.0.0.127:8555/bedroom", e.cameras[1].overlayStream)  // no sd: falls back to hd
        assertEquals(false, e.cameras[1].connected)
        assertEquals(true, e.cameras[1].person)
        assertEquals(false, e.cameras[1].alerts)
    }

    @Test
    fun `person_detected carries name, confidence, streams and snapshot`() {
        val e = HubEvent.parse("person_detected", """
            {"type": "person_detected", "camera": "kitchen", "name": "Kitchen Cam", "confidence": 0.876,
             "since": "2026-10-27T16:42:34.641+00:00",
             "streams": {"hd": "rtsp://10.0.0.127:8555/kitchen", "sd": "rtsp://10.0.0.127:8555/kitchen_sd"},
             "snapshot": "/api/cameras/kitchen/snapshot.jpg?t=1793119354641", "time": "x"}
        """) as HubEvent.PersonDetected
        assertEquals("kitchen", e.camera)
        assertEquals("Kitchen Cam", e.name)
        assertEquals(0.876, e.confidence, 1e-9)
        assertEquals("rtsp://10.0.0.127:8555/kitchen_sd", e.streams["sd"])
        assertEquals("/api/cameras/kitchen/snapshot.jpg?t=1793119354641", e.snapshot)
    }

    @Test
    fun `person_gone, alerts and camera_status`() {
        assertEquals(HubEvent.PersonGone("photo", "alerts_off"),
            HubEvent.parse("person_gone", """{"type": "person_gone", "camera": "photo", "reason": "alerts_off", "duration": 3.2}"""))
        assertEquals(HubEvent.Alerts(false, mapOf("kitchen" to true, "bedroom" to true)),
            HubEvent.parse("alerts", """{"type": "alerts", "enabled": false, "cameras": {"kitchen": true, "bedroom": true}}"""))
        assertEquals(HubEvent.CameraStatus("bedroom", false),
            HubEvent.parse("camera_status", """{"type": "camera_status", "camera": "bedroom", "connected": false}"""))
    }

    @Test
    fun `unknown types and malformed data are ignored`() {
        assertNull(HubEvent.parse("something_new", """{"type": "something_new"}"""))
        assertNull(HubEvent.parse("person_detected", "not json"))
        assertNull(HubEvent.parse("person_detected", """{"type": "person_detected"}"""))  // no camera
    }
}
