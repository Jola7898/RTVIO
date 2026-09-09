package com.rtvio.mapper.service

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log
import androidx.camera.view.PreviewView
import androidx.lifecycle.LifecycleOwner
import com.rtvio.mapper.capture.CameraCapture
import com.rtvio.mapper.data.CameraIntrinsics
import com.rtvio.mapper.data.SettingsManager
import com.rtvio.mapper.data.cameraIntrinsicsFromCharacteristics
import com.rtvio.mapper.net.StreamClient
import com.rtvio.mapper.sensors.GpsCollector
import com.rtvio.mapper.sensors.SensorDataCollector
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * One capture-and-stream session: camera, IMU, GPS and the socket, started and
 * stopped together.
 *
 * Keeping the coordination here rather than in the activity means the start
 * sequence, the WiFi-loss behaviour and the teardown order all live in one
 * readable place, and the activity is left doing nothing but rendering state.
 */
class StreamingSession(
    private val appContext: Context,
    private val settings: SettingsManager
) {

    private companion object {
        const val TAG = "StreamingSession"
    }

    val client = StreamClient(videoCapacity = SettingsManager.VIDEO_QUEUE_CAPACITY)
    val imu = SensorDataCollector(appContext)
    val gps = GpsCollector(appContext)

    private var camera: CameraCapture? = null

    /** Transient notices for the UI: GPS warnings, WiFi loss, camera errors. */
    private val _events = MutableSharedFlow<String>(
        replay = 0, extraBufferCapacity = 32, onBufferOverflow = BufferOverflow.DROP_OLDEST
    )
    val events: SharedFlow<String> = _events.asSharedFlow()

    private val _wifiConnected = MutableStateFlow(true)
    val wifiConnected: StateFlow<Boolean> = _wifiConnected.asStateFlow()

    @Volatile var isStreaming = false
        private set

    private var startedAtMs = 0L
    private var connectivityCallback: ConnectivityManager.NetworkCallback? = null

    val cameraFps: Double get() = camera?.measuredFps ?: 0.0
    val gyroMagnitude: Float get() = imu.gyroMagnitude
    val activeResolution get() = camera?.activeResolution
    val isPreviewRunning: Boolean get() = camera?.isRunning == true

    data class Summary(
        val durationMs: Long,
        val framesSent: Long,
        val framesDropped: Long,
        val imuSamples: Long,
        val gpsFixes: Long,
        val bytesSent: Long
    )

    /**
     * Binds the camera for live preview.
     *
     * Kept separate from [start] so the operator can frame a shot before
     * committing to a session, and so tapping START has no camera-open delay.
     * Encoding stays switched off until [start] flips the gate.
     *
     * Safe to call repeatedly; call it again after a settings change to rebind
     * at the new resolution or frame rate.
     */
    fun startPreview(owner: LifecycleOwner, previewView: PreviewView) {
        camera?.stop()
        val cam = CameraCapture(appContext, owner, previewView)
        camera = cam
        cam.streaming = isStreaming
        cam.start(
            requested = settings.resolution,
            targetFps = settings.targetFps,
            quality = settings.jpegQuality,
            listener = { jpeg, timestampMs ->
                // offerFrame serialises the bytes synchronously, so the
                // encoder's reusable buffer can be handed over as-is - no
                // defensive copy, no per-frame allocation.
                if (_wifiConnected.value) {
                    client.offerFrame(
                        timestampMs, jpeg.width, jpeg.height, jpeg.bytes, jpeg.length
                    )
                }
            },
            onError = { message ->
                _events.tryEmit("Camera error: $message")
                Log.e(TAG, "camera error: $message")
            }
        )
    }

    /** Releases the camera. Never called while streaming. */
    fun stopPreview() {
        camera?.stop()
        camera = null
    }

    /**
     * Runs the spec's start sequence: validate the link, open the socket, start
     * the sensors, then let frames through.
     *
     * @return an error string if the session could not start, null on success.
     */
    fun start(owner: LifecycleOwner, previewView: PreviewView): String? {
        if (isStreaming) return null

        val host = settings.serverIp
        if (host.isEmpty()) return "Set a server IP in Settings first"
        if (!imu.hasAccelerometer) return "This device has no accelerometer"

        // 1. Validate WiFi. Streaming over cellular to a LAN address cannot
        // work, so this is a hard stop rather than a warning.
        registerWifiWatch()
        if (!isWifiConnected()) {
            unregisterWifiWatch()
            return "Connect to WiFi before streaming"
        }

        startedAtMs = System.currentTimeMillis()
        isStreaming = true

        // 2. Socket. Starting it first means the connection is already
        // negotiated by the time the first frame is encoded.
        client.start(
            host = host,
            port = settings.serverPort,
            autoReconnect = settings.autoReconnect,
            baseBackoffSec = settings.reconnectIntervalSec
        )

        // Extract and send camera intrinsics immediately after socket starts.
        // The desktop receiver will use these as ground truth for this phone's camera.
        try {
            val intrinsics = extractCameraIntrinsics()
            if (intrinsics != null) {
                client.offerIntrinsics(
                    fxPix = intrinsics.fx_pix.toFloat(),
                    fyPix = intrinsics.fy_pix.toFloat(),
                    cxPix = intrinsics.cx_pix.toFloat(),
                    cyPix = intrinsics.cy_pix.toFloat(),
                    k1 = intrinsics.k1,
                    k2 = intrinsics.k2,
                    p1 = intrinsics.p1,
                    p2 = intrinsics.p2,
                    k3 = intrinsics.k3,
                    source = intrinsics.source
                )
                Log.i(TAG, "Sent camera intrinsics: ${intrinsics.source}")
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not extract camera intrinsics", e)
        }

        // 3. Camera. Already bound for preview; opening the gate is all that
        // is left, so the first frame goes out within one capture period.
        if (camera == null) startPreview(owner, previewView)
        camera?.updateQuality(settings.jpegQuality)
        camera?.streaming = true

        // 4. Sensors. A missing gyroscope is not fatal - samples still carry
        // acceleration - but it removes the angular-rate signal a VIO pipeline
        // leans on hardest, so say so up front rather than let the desktop
        // discover a stream of zeros.
        if (!imu.hasGyroscope) {
            _events.tryEmit("No gyroscope on this device - angular rate will be zero")
        }
        imu.start(settings.imuRateHz, settings.imuBatchIntervalMs) { batch ->
            if (_wifiConnected.value) client.offerImuBatch(batch)
        }

        if (settings.outdoorMode) {
            gps.start(
                onFix = { fix ->
                    if (_wifiConnected.value) {
                        client.offerGps(
                            timestampMs = fix.time,
                            latitude = fix.latitude,
                            longitude = fix.longitude,
                            altitudeM = fix.altitude.toFloat(),
                            accuracyM = if (fix.hasAccuracy()) fix.accuracy else -1f
                        )
                    }
                },
                onStatus = { _events.tryEmit(it) }
            )
        }

        StreamingForegroundService.start(
            appContext,
            needsLocation = settings.outdoorMode,
            keepAwake = settings.keepAwake
        )
        return null
    }

    /** Tears the session down in reverse order and reports what it moved. */
    fun stop(): Summary {
        if (!isStreaming) {
            return Summary(0, 0, 0, 0, 0, 0)
        }
        isStreaming = false

        // The preview stays bound: the operator is usually still looking at the
        // scene, and a rebind here would black the viewfinder for a moment.
        camera?.streaming = false
        imu.stop()
        gps.stop()

        // Read the stats *after* stopping: the client publishes on a 1 Hz
        // ticker, and its stop() pushes one final snapshot. Reading first would
        // undercount the session by up to a second of frames.
        client.stop()
        val s = client.stats.value
        val summary = Summary(
            durationMs = System.currentTimeMillis() - startedAtMs,
            framesSent = s.framesSent,
            framesDropped = s.framesDropped,
            imuSamples = s.imuSamplesSent,
            gpsFixes = s.gpsFixesSent,
            bytesSent = s.bytesSent
        )

        unregisterWifiWatch()
        StreamingForegroundService.stop(appContext)
        return summary
    }

    /** Applies a settings change that does not require restarting the camera. */
    fun applyQuality() = camera?.updateQuality(settings.jpegQuality)

    /**
     * Extract camera intrinsics from the device's Camera2 characteristics.
     * Returns null if the characteristics cannot be obtained.
     */
    private fun extractCameraIntrinsics(): CameraIntrinsics? {
        return try {
            val cm = appContext.getSystemService(Context.CAMERA_SERVICE) as CameraManager
            val cameraId = cm.cameraIdList.firstOrNull {
                cm.getCameraCharacteristics(it)
                    .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
            } ?: cm.cameraIdList.firstOrNull() ?: return null

            val characteristics = cm.getCameraCharacteristics(cameraId)
            cameraIntrinsicsFromCharacteristics(
                characteristics,
                settings.resolution.width,
                settings.resolution.height
            )
        } catch (e: Exception) {
            Log.w(TAG, "Failed to extract camera intrinsics", e)
            null
        }
    }

    // ------------------------------------------------------------------ wifi

    private fun connectivityManager() =
        appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    /**
     * True when the active network is WiFi.
     *
     * Deliberately does not require NET_CAPABILITY_INTERNET: a field setup is
     * often a router or phone hotspot with no uplink at all, and that is a
     * perfectly good network for reaching a desktop on the same LAN.
     */
    fun isWifiConnected(): Boolean = try {
        val cm = connectivityManager()
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
    } catch (e: Exception) {
        Log.w(TAG, "connectivity check failed", e)
        false
    }

    /**
     * Watches specifically for WiFi. Losing it pauses the feed (spec 10.6)
     * rather than letting frames pile into a socket that cannot reach a LAN
     * address any more.
     */
    private fun registerWifiWatch() {
        if (connectivityCallback != null) return
        _wifiConnected.value = isWifiConnected()

        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (!_wifiConnected.value) {
                    _wifiConnected.value = true
                    _events.tryEmit("WiFi reconnected - resuming")
                }
            }

            override fun onLost(network: Network) {
                // onLost fires per network; only report a loss if nothing is
                // left, otherwise a band switch would look like an outage.
                if (!isWifiConnected()) {
                    _wifiConnected.value = false
                    _events.tryEmit("WiFi disconnected - streaming paused")
                }
            }
        }
        connectivityCallback = callback
        try {
            connectivityManager().registerNetworkCallback(
                NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .build(),
                callback
            )
        } catch (e: Exception) {
            Log.w(TAG, "could not register network callback", e)
            connectivityCallback = null
        }
    }

    private fun unregisterWifiWatch() {
        connectivityCallback?.let {
            try {
                connectivityManager().unregisterNetworkCallback(it)
            } catch (e: Exception) {
                Log.w(TAG, "unregister network callback failed", e)
            }
        }
        connectivityCallback = null
        _wifiConnected.value = true
    }
}
