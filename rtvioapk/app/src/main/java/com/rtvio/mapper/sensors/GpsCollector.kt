package com.rtvio.mapper.sensors

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.concurrent.atomic.AtomicLong

/**
 * GNSS fixes for outdoor mode.
 *
 * Deliberately uses the platform LocationManager on GPS_PROVIDER rather than
 * fused location: fused blends in WiFi and cell positioning, which produces
 * plausible-looking fixes that are metres to hundreds of metres wrong and carry
 * an optimistic accuracy figure. For georeferencing a reconstruction, a real
 * satellite fix or none at all is the only useful contract.
 */
class GpsCollector(private val context: Context) {

    private companion object {
        const val TAG = "GpsCollector"
        const val MIN_INTERVAL_MS = 1000L
        /** Spec section 10.5: warn, but keep streaming, after this long with no fix. */
        const val NO_FIX_WARNING_MS = 60_000L
        const val WARNING_CHECK_MS = 5_000L
    }

    private val locationManager =
        context.getSystemService(Context.LOCATION_SERVICE) as LocationManager

    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var listener: LocationListener? = null

    private val lastFixAtMs = AtomicLong(0)

    @Volatile var lastAccuracyM: Float = -1f
        private set
    @Volatile private var warned = false

    val hasFix: Boolean get() = lastFixAtMs.get() > 0

    /** Milliseconds since the last fix, or -1 if there has never been one. */
    val fixAgeMs: Long
        get() = lastFixAtMs.get().let { if (it == 0L) -1L else System.currentTimeMillis() - it }

    val isProviderEnabled: Boolean
        get() = try {
            locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)
        } catch (e: Exception) {
            false
        }

    fun hasPermission(): Boolean = ContextCompat.checkSelfPermission(
        context, Manifest.permission.ACCESS_FINE_LOCATION
    ) == PackageManager.PERMISSION_GRANTED

    /**
     * @param onFix fires on the collector thread for every satellite fix.
     * @param onStatus fires for conditions the operator should see - GPS off,
     *   permission missing, or no fix for a minute.
     */
    fun start(onFix: (Location) -> Unit, onStatus: (String) -> Unit) {
        if (thread != null) return
        if (!hasPermission()) {
            onStatus("Location permission not granted - GPS disabled")
            return
        }
        if (!isProviderEnabled) {
            onStatus("GPS is turned off in system settings")
            // Still register: the user may enable it mid-session and the
            // provider will start delivering without us doing anything.
        }

        warned = false
        lastFixAtMs.set(0)

        val t = HandlerThread("rtvio-gps")
        t.start()
        thread = t
        val h = Handler(t.looper)
        handler = h

        val l = object : LocationListener {
            override fun onLocationChanged(location: Location) {
                lastFixAtMs.set(System.currentTimeMillis())
                lastAccuracyM = if (location.hasAccuracy()) location.accuracy else -1f
                if (warned) {
                    warned = false
                    onStatus("GPS fix reacquired")
                }
                onFix(location)
            }

            // Abstract below API 30, so it must be implemented even though the
            // platform stopped delivering it from API 30 onward.
            @Suppress("OVERRIDE_DEPRECATION")
            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {
            }

            override fun onProviderEnabled(provider: String) {
                onStatus("GPS enabled")
            }

            override fun onProviderDisabled(provider: String) {
                onStatus("GPS disabled - fixes will stop")
            }
        }
        listener = l

        try {
            locationManager.requestLocationUpdates(
                LocationManager.GPS_PROVIDER, MIN_INTERVAL_MS, 0f, l, t.looper
            )
        } catch (e: SecurityException) {
            onStatus("Location permission revoked")
            stop()
            return
        } catch (e: IllegalArgumentException) {
            onStatus("This device has no GPS provider")
            stop()
            return
        }

        h.postDelayed(object : Runnable {
            override fun run() {
                val age = fixAgeMs
                // Never had a fix, or had one and lost it for a minute.
                val stale = age < 0 || age > NO_FIX_WARNING_MS
                if (stale && !warned) {
                    warned = true
                    onStatus("No GPS fix for over 60 s - streaming continues without georeferencing")
                }
                handler?.postDelayed(this, WARNING_CHECK_MS)
            }
        }, NO_FIX_WARNING_MS)
    }

    fun stop() {
        listener?.let {
            try {
                locationManager.removeUpdates(it)
            } catch (e: Exception) {
                Log.w(TAG, "removeUpdates failed", e)
            }
        }
        listener = null
        handler?.removeCallbacksAndMessages(null)
        thread?.quitSafely()
        thread = null
        handler = null
        lastAccuracyM = -1f
    }
}
