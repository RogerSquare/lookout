package dev.rogersquare.lookout

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Starts the alert listener when the projector boots, and again right after the app is updated
 * (an install stops the running service). Both broadcasts may start a foreground service.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> LookoutService.start(context)
        }
    }
}
