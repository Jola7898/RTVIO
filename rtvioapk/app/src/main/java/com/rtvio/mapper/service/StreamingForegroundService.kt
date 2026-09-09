package com.rtvio.mapper.service

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.rtvio.mapper.R
import com.rtvio.mapper.ui.MainActivity

/**
 * Keeps the capture pipeline alive while the app is not in the foreground.
 *
 * The camera, sensors and socket all live in the activity's session object;
 * this service exists purely to hold the process in a state Android will not
 * kill mid-survey, and to hold a wake lock so the CPU keeps encoding when the
 * screen turns off.
 *
 * The foreground service *types* are declared at runtime rather than taken
 * wholesale from the manifest: from Android 14, starting with the `location`
 * type without the location permission granted throws, so indoor mode must not
 * ask for it.
 */
class StreamingForegroundService : Service() {

    companion object {
        private const val TAG = "StreamingService"
        private const val CHANNEL_ID = "rtvio_streaming"
        private const val NOTIFICATION_ID = 0x8100

        const val ACTION_START = "com.rtvio.mapper.START"
        const val ACTION_STOP = "com.rtvio.mapper.STOP"
        const val ACTION_UPDATE = "com.rtvio.mapper.UPDATE"

        const val EXTRA_TEXT = "text"
        const val EXTRA_NEEDS_LOCATION = "needs_location"
        const val EXTRA_KEEP_AWAKE = "keep_awake"

        /**
         * Invoked when the user taps Stop on the notification.
         *
         * The camera, sensors and socket are owned by the UI's session object,
         * not by this service, so stopping the service alone would leave the
         * pipeline running with nothing on screen to stop it. The UI registers
         * a teardown here for the duration of a session.
         */
        @Volatile
        var onStopRequested: (() -> Unit)? = null

        fun start(context: Context, needsLocation: Boolean, keepAwake: Boolean) {
            val intent = Intent(context, StreamingForegroundService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_NEEDS_LOCATION, needsLocation)
                putExtra(EXTRA_KEEP_AWAKE, keepAwake)
            }
            ContextCompat.startForegroundService(context, intent)
        }

        fun update(context: Context, text: String) {
            val intent = Intent(context, StreamingForegroundService::class.java).apply {
                action = ACTION_UPDATE
                putExtra(EXTRA_TEXT, text)
            }
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, StreamingForegroundService::class.java))
        }
    }

    private var wakeLock: PowerManager.WakeLock? = null

    /** Foreground service types settled at start and reused on every update. */
    private var foregroundTypes = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                onStopRequested?.invoke()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_UPDATE -> {
                // Deliberately startForeground again rather than a bare notify:
                // this arrives via startForegroundService, and the platform
                // requires a startForeground call for each one. A plain notify
                // would leave that promise unkept and Android would kill the
                // process with ForegroundServiceDidNotStartInTimeException.
                // Re-calling startForeground on an already-foreground service
                // simply swaps the notification.
                enterForeground(intent.getStringExtra(EXTRA_TEXT).orEmpty())
                return START_STICKY
            }
            else -> {
                val needsLocation = intent?.getBooleanExtra(EXTRA_NEEDS_LOCATION, false) ?: false
                foregroundTypes = resolveForegroundTypes(needsLocation)
                enterForeground(getString(R.string.notif_starting))
                if (intent?.getBooleanExtra(EXTRA_KEEP_AWAKE, true) != false) acquireWakeLock()
            }
        }
        return START_STICKY
    }

    /**
     * From Android 14 a type may only be claimed while its permission is held,
     * so the set is computed from what was actually granted rather than taken
     * wholesale from the manifest.
     */
    private fun resolveForegroundTypes(needsLocation: Boolean): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return 0
        var t = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        if (hasPermission(Manifest.permission.CAMERA)) {
            t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        }
        if (needsLocation && hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) {
            t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        }
        return t
    }

    private fun enterForeground(text: String) {
        val notification = buildNotification(text)
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, foregroundTypes)
        } catch (e: Exception) {
            // On API 34+ this throws if a declared type lacks its permission.
            // Falling back to the plain data-sync type still keeps the process
            // alive, which is the point of the service.
            Log.w(TAG, "typed startForeground rejected, falling back", e)
            ServiceCompat.startForeground(
                this, NOTIFICATION_ID, notification,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
            )
        }
    }

    private fun hasPermission(permission: String) =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    @Suppress("WakelockTimeout")
    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        // No timeout: the lock's lifetime is the streaming session, which the
        // user ends explicitly, and onDestroy releases it unconditionally.
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rtvio:streaming").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, StreamingForegroundService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_stream)
            .setOngoing(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(open)
            .addAction(0, getString(R.string.action_stop_notification), stop)
            .build()
    }

    private fun notificationManager() =
        getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notif_channel),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.notif_channel_desc)
            setShowBadge(false)
        }
        notificationManager()?.createNotificationChannel(channel)
    }

    override fun onDestroy() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        super.onDestroy()
    }

    /**
     * A swipe-away of the task should not leave the pipeline running headless.
     */
    override fun onTaskRemoved(rootIntent: Intent?) {
        onStopRequested?.invoke()
        stopSelf()
        super.onTaskRemoved(rootIntent)
    }
}
