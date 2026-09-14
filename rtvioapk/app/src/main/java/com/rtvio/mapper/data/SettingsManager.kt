package com.rtvio.mapper.data

import android.content.Context
import android.content.SharedPreferences
import android.util.Size
import androidx.preference.PreferenceManager

/**
 * Typed facade over the default SharedPreferences.
 *
 * The settings screen writes through androidx.preference (which stores list
 * choices and text fields as Strings); everything else in the app reads through
 * here so the string-to-int coercions live in exactly one place.
 */
class SettingsManager(context: Context) {

    private val prefs: SharedPreferences =
        PreferenceManager.getDefaultSharedPreferences(context.applicationContext)

    companion object {
        const val KEY_SERVER_IP = "server_ip"
        const val KEY_SERVER_PORT = "server_port"
        const val KEY_RESOLUTION = "video_resolution"
        const val KEY_ASPECT = "video_aspect"
        const val KEY_FPS = "video_fps"
        const val KEY_JPEG_QUALITY = "jpeg_quality"
        const val KEY_IMU_RATE = "imu_rate_hz"
        const val KEY_IMU_BATCH_MS = "imu_batch_ms"
        const val KEY_OUTDOOR_MODE = "outdoor_mode"
        const val KEY_AUTO_RECONNECT = "auto_reconnect"
        const val KEY_RECONNECT_INTERVAL = "reconnect_interval"
        const val KEY_SHOW_FPS_OVERLAY = "show_fps_overlay"
        const val KEY_SHOW_STATS = "show_stats"
        const val KEY_KEEP_AWAKE = "keep_awake"
        const val KEY_HAPTICS = "haptics"

        const val DEFAULT_PORT = 5555
        /** Video frames buffered before the oldest is dropped (spec section 10.1). */
        const val VIDEO_QUEUE_CAPACITY = 10
    }

    var serverIp: String
        get() = prefs.getString(KEY_SERVER_IP, "").orEmpty().trim()
        set(value) = prefs.edit().putString(KEY_SERVER_IP, value.trim()).apply()

    /** Clamped into the spec's 1024-65535 range; a junk value falls back to 5555. */
    var serverPort: Int
        get() {
            val raw = prefs.getString(KEY_SERVER_PORT, DEFAULT_PORT.toString())
                ?.toIntOrNull() ?: DEFAULT_PORT
            return raw.coerceIn(1024, 65535)
        }
        set(value) = prefs.edit()
            .putString(KEY_SERVER_PORT, value.coerceIn(1024, 65535).toString()).apply()

    /**
     * "720p" or "1080p" - the long edge of the frame (1280 / 1920 px). 720p is
     * the default because the desktop's VGGT resizes every frame to 518 px
     * anyway: a bigger frame only costs encode time and WiFi bandwidth, which
     * are exactly what limit how many frames reach the desktop.
     */
    var resolutionKey: String
        get() = prefs.getString(KEY_RESOLUTION, "720p") ?: "720p"
        set(value) {
            if (value == "720p" || value == "1080p") prefs.edit().putString(KEY_RESOLUTION, value).apply()
        }

    /**
     * 4:3 is the camera sensor's native shape - the full field of view. 16:9
     * crops it. More view per frame means more overlap between frames, which
     * reconstruction wants, so 4:3 is the default.
     */
    var aspect43: Boolean
        get() = prefs.getString(KEY_ASPECT, "4:3") != "16:9"
        set(value) = prefs.edit().putString(KEY_ASPECT, if (value) "4:3" else "16:9").apply()

    /** Requested capture size (landscape, sensor orientation). */
    val resolution: Size
        get() {
            val long = if (resolutionKey == "1080p") 1920 else 1280
            return Size(long, if (aspect43) long * 3 / 4 else long * 9 / 16)
        }

    var targetFps: Int
        get() = prefs.getString(KEY_FPS, "30")?.toIntOrNull() ?: 30
        set(value) = prefs.edit().putString(KEY_FPS, value.coerceIn(5, 60).toString()).apply()

    /** JPEG quality percent, 30-95. */
    var jpegQuality: Int
        get() = prefs.getInt(KEY_JPEG_QUALITY, 85).coerceIn(30, 95)
        set(value) = prefs.edit().putInt(KEY_JPEG_QUALITY, value.coerceIn(30, 95)).apply()

    /** Requested inertial sampling rate in Hz. */
    val imuRateHz: Int
        get() = prefs.getString(KEY_IMU_RATE, "100")?.toIntOrNull() ?: 100

    /** How often a batch of IMU samples is flushed to the socket. */
    val imuBatchIntervalMs: Long
        get() = (prefs.getString(KEY_IMU_BATCH_MS, "50")?.toLongOrNull() ?: 50L)
            .coerceIn(10L, 500L)

    /** Outdoor mode streams GPS; indoor mode leaves the GNSS radio alone. */
    val outdoorMode: Boolean
        get() = prefs.getBoolean(KEY_OUTDOOR_MODE, true)

    val autoReconnect: Boolean
        get() = prefs.getBoolean(KEY_AUTO_RECONNECT, true)

    /** Base delay for reconnect backoff, in seconds (1-30). */
    val reconnectIntervalSec: Int
        get() = (prefs.getString(KEY_RECONNECT_INTERVAL, "5")?.toIntOrNull() ?: 5)
            .coerceIn(1, 30)

    val showFpsOverlay: Boolean get() = prefs.getBoolean(KEY_SHOW_FPS_OVERLAY, true)
    val showStats: Boolean get() = prefs.getBoolean(KEY_SHOW_STATS, true)
    val keepAwake: Boolean get() = prefs.getBoolean(KEY_KEEP_AWAKE, true)
    val haptics: Boolean get() = prefs.getBoolean(KEY_HAPTICS, true)

    /**
     * Rough Mbps a given configuration will ask of the link.
     *
     * Derived from measured JPEG behaviour rather than theory: a 4:2:0 frame
     * compresses to roughly (pixels * bitsPerPixel / 8) bytes, where bits per
     * pixel runs about 0.15 at quality 30 and about 0.75 at quality 90 on
     * typical outdoor scenes. Detailed scenes (foliage, gravel) land above this
     * estimate, flat ones well below - it is for sizing the link, not billing.
     */
    fun estimateMbps(
        size: Size = resolution,
        fps: Int = targetFps,
        quality: Int = jpegQuality
    ): Double {
        val bitsPerPixel = 0.15 + (quality - 30).coerceIn(0, 60) / 60.0 * 0.60
        val bytesPerFrame = size.width.toDouble() * size.height * bitsPerPixel / 8.0
        val videoBits = bytesPerFrame * fps * 8
        // IMU adds imuRateHz * 32 bytes/s plus batch headers; GPS is negligible.
        val imuBits = imuRateHz * 32.0 * 8
        return (videoBits + imuBits) / 1_000_000.0
    }
}
