package com.rtvio.mapper.service

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.location.Location
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.util.Size
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import com.rtvio.mapper.BuildConfig
import com.rtvio.mapper.capture.CameraCapture
import com.rtvio.mapper.capture.LocalSessionRecorder
import com.rtvio.mapper.data.CameraIntrinsics
import com.rtvio.mapper.data.LocalSessions
import com.rtvio.mapper.data.SettingsManager
import com.rtvio.mapper.data.cameraIntrinsicsForOutput
import com.rtvio.mapper.net.ConnectionInfo
import com.rtvio.mapper.net.ConnectionState
import com.rtvio.mapper.net.FrameSpool
import com.rtvio.mapper.net.ImuSample
import com.rtvio.mapper.net.StreamClient
import com.rtvio.mapper.sensors.GpsCollector
import com.rtvio.mapper.sensors.SensorDataCollector
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Where a session stands.
 *
 *   OFF         not connected
 *   CONNECTING  socket starting / reconnecting, receiver version not known yet
 *   ARMED       connected to RTVIO Studio (protocol v2): viewfinder images go to
 *               the desktop, nothing is recorded, waiting for START
 *   RECORDING   every frame goes to the on-phone spool and on to the desktop
 *   FINISHING   STOP received: camera gate closed, still uploading the spool
 *   STREAMING   connected to a v1 receiver (live_pipeline.py / mock_receiver):
 *               the original live stream, frames dropped when the link lags
 */
enum class LinkState { OFF, CONNECTING, ARMED, RECORDING, FINISHING, STREAMING }

/**
 * One connection to the desktop and everything captured over it: camera, IMU,
 * GPS and the socket. With RTVIO Studio on the other end, recordings are started
 * and stopped by the desktop (or the phone's REC button) and a single
 * connection can carry any number of them.
 *
 * All state changes happen on the main thread: commands from the socket and
 * the 1 Hz ticker are posted there, so the camera rebinds and the state
 * machine never race each other.
 */
class StreamingSession(
    private val appContext: Context,
    private val settings: SettingsManager
) {

    private companion object {
        const val TAG = "StreamingSession"
        const val TICK_MS = 1000L
    }

    val client = StreamClient(videoCapacity = SettingsManager.VIDEO_QUEUE_CAPACITY)
    val imu = SensorDataCollector(appContext)
    val gps = GpsCollector(appContext)

    private var camera: CameraCapture? = null
    private var boundConfig: Triple<Size, Int, Boolean>? = null
    private var owner: LifecycleOwner? = null
    private var previewView: PreviewView? = null
    private val main = Handler(Looper.getMainLooper())
    private var scope: CoroutineScope? = null

    /** Transient notices for the UI: GPS warnings, WiFi loss, camera errors. */
    private val _events = MutableSharedFlow<String>(
        replay = 0, extraBufferCapacity = 32, onBufferOverflow = BufferOverflow.DROP_OLDEST
    )
    val events: SharedFlow<String> = _events.asSharedFlow()

    private val _wifiConnected = MutableStateFlow(true)
    val wifiConnected: StateFlow<Boolean> = _wifiConnected.asStateFlow()

    private val _state = MutableStateFlow(LinkState.OFF)
    val state: StateFlow<LinkState> = _state.asStateFlow()

    /** True from CONNECT until DISCONNECT. */
    val isStreaming: Boolean get() = _state.value != LinkState.OFF

    @Volatile private var localRecorder: LocalSessionRecorder? = null
    private var localScope: CoroutineScope? = null
    private val _localRecording = MutableStateFlow(false)
    val localRecording: StateFlow<Boolean> = _localRecording.asStateFlow()
    val isLocalRecording: Boolean get() = localRecorder != null
    /** True whenever the camera/sensors must stay bound: connected to the
     *  desktop, or recording fully offline to phone storage. */
    val isCapturing: Boolean get() = isStreaming || isLocalRecording
    @Volatile var localSessionId: String? = null
        private set
    @Volatile var localRecordingStartedAtMs = 0L
        private set

    @Volatile var sessionId: String? = null
        private set
    @Volatile var recordingStartedAtMs = 0L
        private set
    private var lastCompleted: String? = null
    private var lastFramesSent = 0L
    private var lastError: String? = null
    private var activeParams: JSONObject? = null
    private var intrinsicsSent = false

    private var startedAtMs = 0L
    private var connectivityCallback: ConnectivityManager.NetworkCallback? = null

    val cameraFps: Double get() = camera?.measuredFps ?: 0.0
    val gyroMagnitude: Float get() = imu.gyroMagnitude
    val activeResolution get() = camera?.outputSize ?: camera?.activeResolution
    val isPreviewRunning: Boolean get() = camera?.isRunning == true
    val recordingBacklog: Int get() = client.recordingBacklog
    val framesCaptured: Long get() = camera?.capturedCount?.get() ?: 0L
    val framesSkipped: Long get() = camera?.skippedCount?.get() ?: 0L

    data class Summary(
        val durationMs: Long,
        val framesSent: Long,
        val framesDropped: Long,
        val imuSamples: Long,
        val gpsFixes: Long,
        val bytesSent: Long
    )

    // ---------------------------------------------------------------- camera

    /**
     * Binds the camera for live preview. Kept separate from [start] so the
     * operator can frame a shot before connecting, and so START has no
     * camera-open delay. Call again to rebind at a new resolution/frame rate.
     */
    fun startPreview(owner: LifecycleOwner, previewView: PreviewView) {
        this.owner = owner
        this.previewView = previewView
        camera?.stop()
        val cam = CameraCapture(appContext, owner, previewView)
        camera = cam
        val cfg = Triple(settings.resolution, settings.targetFps, settings.aspect43)
        boundConfig = cfg
        intrinsicsSent = false
        cam.streaming = _state.value == LinkState.RECORDING || _state.value == LinkState.STREAMING
        cam.previewListener = CameraCapture.FrameListener { jpeg, ts ->
            if (_state.value == LinkState.ARMED) {
                client.offerPreview(ts, jpeg.width, jpeg.height, jpeg.bytes, jpeg.length)
            }
        }
        cam.start(
            requested = cfg.first,
            targetFps = cfg.second,
            quality = settings.jpegQuality,
            aspect43 = cfg.third,
            listener = { jpeg, timestampMs -> onFrame(jpeg.bytes, jpeg.length, jpeg.width, jpeg.height, timestampMs) },
            onError = { message ->
                _events.tryEmit("Camera error: $message")
                Log.e(TAG, "camera error: $message")
            }
        )
    }

    private fun onFrame(bytes: ByteArray, length: Int, w: Int, h: Int, timestampMs: Long) {
        localRecorder?.let {
            it.onFrame(bytes, length, timestampMs)
            return
        }
        // offerFrame serialises synchronously, so the encoder's reusable
        // buffer can be handed over as-is.
        when (_state.value) {
            // FINISHING too: frames already inside the encoder when STOP
            // arrived were captured during the take and belong to it.
            LinkState.RECORDING, LinkState.FINISHING -> client.offerFrame(timestampMs, w, h, bytes, length)
            LinkState.STREAMING -> if (_wifiConnected.value) client.offerFrame(timestampMs, w, h, bytes, length)
            else -> Unit
        }
    }

    /** Releases the camera. Never called while recording. */
    fun stopPreview() {
        camera?.stop()
        camera = null
        boundConfig = null
    }

    // -------------------------------------------------------------- connect

    /**
     * CONNECT: validate the link and open the socket. What happens next depends
     * on who answers - see [onConnection].
     *
     * @return an error string if the session could not start, null on success.
     */
    fun start(owner: LifecycleOwner, previewView: PreviewView): String? {
        if (_state.value != LinkState.OFF) return null
        if (localRecorder != null) return "Stop local recording first"
        val host = settings.serverIp
        if (host.isEmpty()) return "Set a server IP in Settings first"

        // Streaming over cellular to a LAN address cannot work, so this is a
        // hard stop rather than a warning.
        registerWifiWatch()
        if (!isWifiConnected()) {
            unregisterWifiWatch()
            return "Connect to WiFi first"
        }

        startedAtMs = System.currentTimeMillis()
        lastCompleted = null
        lastError = null
        _state.value = LinkState.CONNECTING
        val s = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        scope = s

        client.onCommand = { json -> main.post { handleCommand(json) } }
        client.start(
            host = host,
            port = settings.serverPort,
            autoReconnect = settings.autoReconnect,
            baseBackoffSec = settings.reconnectIntervalSec
        )
        s.launch { client.connection.collect { info -> main.post { onConnection(info) } } }
        s.launch {
            while (isActive) {
                delay(TICK_MS)
                main.post { tick() }
            }
        }
        if (camera == null) startPreview(owner, previewView)

        StreamingForegroundService.start(
            appContext,
            needsLocation = settings.outdoorMode,
            keepAwake = settings.keepAwake
        )
        return null
    }

    private fun onConnection(info: ConnectionInfo) {
        if (_state.value == LinkState.OFF) return
        if (info.state == ConnectionState.CONNECTED) {
            intrinsicsSent = false
            if (info.remoteControl) {
                if (_state.value == LinkState.CONNECTING) {
                    _state.value = LinkState.ARMED
                    _events.tryEmit("Connected to RTVIO Studio - waiting for START")
                }
                // RECORDING / FINISHING across a reconnect: nothing to change,
                // the spool simply resumes draining and the status below tells
                // the desktop which take the incoming frames belong to.
                sendStatus()
            } else if (_state.value == LinkState.CONNECTING) {
                beginLiveStreaming()
            }
        } else if (_state.value == LinkState.ARMED) {
            _state.value = LinkState.CONNECTING
        }
    }

    /** A v1 receiver: the original behaviour, streaming from the moment of connect. */
    private fun beginLiveStreaming() {
        _state.value = LinkState.STREAMING
        camera?.updateQuality(settings.jpegQuality)
        camera?.streaming = true
        startSensors(imuWanted = true, gpsWanted = settings.outdoorMode)
        _events.tryEmit("Live streaming (this receiver has no remote control)")
    }

    // ------------------------------------------------------------- commands

    private fun handleCommand(json: String) {
        val obj = try {
            JSONObject(json)
        } catch (e: Exception) {
            Log.w(TAG, "unparseable command: $json")
            return
        }
        when (obj.optString("cmd")) {
            "start" -> remoteStart(obj)
            "stop" -> {
                val id = obj.optString("session")
                if (_state.value == LinkState.RECORDING && (id.isEmpty() || id == sessionId)) {
                    endRecording()
                } else {
                    sendStatus()
                }
            }
            "ping" -> sendStatus()
        }
    }

    private fun remoteStart(obj: JSONObject) {
        val id = obj.optString("session").ifEmpty { newSessionId() }
        if (_state.value != LinkState.ARMED) {
            sendStatus(rejected = id, error = "phone is ${_state.value.name.lowercase()}")
            return
        }
        // The desktop's capture settings persist, so the phone's own settings
        // screen afterwards shows what the take was recorded with.
        obj.optString("resolution").takeIf { it.isNotEmpty() }?.let { settings.resolutionKey = it }
        obj.optString("aspect").takeIf { it.isNotEmpty() }?.let { settings.aspect43 = it != "16:9" }
        if (obj.has("fps")) settings.targetFps = obj.optInt("fps", settings.targetFps)
        if (obj.has("jpeg_quality")) settings.jpegQuality = obj.optInt("jpeg_quality", settings.jpegQuality)
        beginRecording(
            id = id,
            gpsWanted = obj.optBoolean("gps", settings.outdoorMode),
            imuWanted = obj.optBoolean("imu", true),
            params = obj
        )
    }

    // ------------------------------------------------------------ recording

    /** The phone's REC button, when connected to RTVIO Studio. */
    fun toggleRecording() {
        when (_state.value) {
            LinkState.ARMED -> beginRecording()
            LinkState.RECORDING -> endRecording()
            else -> Unit
        }
    }

    private fun beginRecording(
        id: String = newSessionId(),
        gpsWanted: Boolean = settings.outdoorMode,
        imuWanted: Boolean = true,
        params: JSONObject? = null
    ) {
        if (_state.value != LinkState.ARMED) return
        val spool = try {
            FrameSpool(File(appContext.filesDir, "spool/$id.bin"))
        } catch (e: Exception) {
            sendStatus(rejected = id, error = "cannot open spool: ${e.message}")
            return
        }
        sessionId = id
        recordingStartedAtMs = System.currentTimeMillis()
        lastError = null
        activeParams = (params ?: JSONObject()).apply {
            remove("cmd"); remove("session")
            put("resolution", settings.resolutionKey)
            put("aspect", if (settings.aspect43) "4:3" else "16:9")
            put("fps", settings.targetFps)
            put("jpeg_quality", settings.jpegQuality)
            put("gps", gpsWanted)
            put("imu", imuWanted)
        }
        client.beginRecording(spool)
        _state.value = LinkState.RECORDING

        val wanted = Triple(settings.resolution, settings.targetFps, settings.aspect43)
        val o = owner
        val pv = previewView
        if ((camera == null || boundConfig != wanted) && o != null && pv != null) {
            startPreview(o, pv)            // new camera starts with streaming = true
        } else {
            camera?.updateQuality(settings.jpegQuality)
            camera?.resetCounters()
            camera?.streaming = true
        }
        startSensors(imuWanted, gpsWanted)
        sendStatus()
        _events.tryEmit("Recording $id")
    }

    fun endRecording() {
        if (_state.value != LinkState.RECORDING) return
        camera?.streaming = false
        stopSensors()
        _state.value = LinkState.FINISHING
        sendStatus()
        val backlog = client.recordingBacklog
        if (backlog > 0) _events.tryEmit("Recording stopped - uploading $backlog queued frames")
        checkFinished()
    }

    /**
     * Closes the take once every frame is on the socket: nothing left in the
     * encoder and nothing left in the spool. The STATUS sent here carries
     * last_completed, which is what tells the desktop the take is whole.
     */
    private fun checkFinished() {
        if (_state.value != LinkState.FINISHING) return
        if (client.recordingBacklog > 0 || (camera?.framesInFlight ?: 0) > 0) return
        val sent = client.recordingFramesSent
        client.finishRecording()?.close(deleteFile = true)
        lastCompleted = sessionId
        lastFramesSent = sent
        _events.tryEmit("Take $sessionId complete: $sent frames sent")
        sessionId = null
        activeParams = null
        _state.value = if (client.connection.value.remoteControl) LinkState.ARMED else LinkState.CONNECTING
        sendStatus()
    }

    private fun startSensors(
        imuWanted: Boolean,
        gpsWanted: Boolean,
        onImu: (List<ImuSample>) -> Unit = { batch ->
            if (_state.value != LinkState.STREAMING || _wifiConnected.value) client.offerImuBatch(batch)
        },
        onGps: (Location) -> Unit = { fix ->
            client.offerGps(
                timestampMs = fix.time,
                latitude = fix.latitude,
                longitude = fix.longitude,
                altitudeM = fix.altitude.toFloat(),
                accuracyM = if (fix.hasAccuracy()) fix.accuracy else -1f
            )
        }
    ) {
        if (imuWanted && imu.hasAccelerometer) {
            imu.start(settings.imuRateHz, settings.imuBatchIntervalMs, onImu)
        }
        if (gpsWanted) {
            gps.start(onFix = onGps, onStatus = { _events.tryEmit(it) })
        }
    }

    private fun stopSensors() {
        imu.stop()
        gps.stop()
    }

    // ------------------------------------------------------------- ticking

    private fun tick() {
        if (_state.value == LinkState.OFF) return
        checkFinished()
        if (!intrinsicsSent) sendIntrinsics()
        sendStatus()
    }

    private fun sendStatus(rejected: String? = null, error: String? = null) {
        if (!client.connection.value.remoteControl) return
        if (error != null) lastError = error
        try {
            val cam = camera
            val o = JSONObject()
            o.put("state", when (_state.value) {
                LinkState.ARMED -> "armed"
                LinkState.RECORDING -> "recording"
                LinkState.FINISHING -> "finishing"
                LinkState.STREAMING -> "streaming"
                LinkState.CONNECTING -> "connecting"
                LinkState.OFF -> "off"
            })
            sessionId?.let { o.put("session", it) }
            activeParams?.let { o.put("params", it) }
            o.put("captured", cam?.capturedCount?.get() ?: 0L)
            o.put("encoded", cam?.encodedCount?.get() ?: 0L)
            o.put("skipped", cam?.skippedCount?.get() ?: 0L)
            o.put("queued", client.recordingBacklog)
            o.put("queued_mb", client.recordingBacklogBytes / 1_000_000.0)
            o.put("sent", client.recordingFramesSent)
            o.put("refused", client.recordingFramesRefused)
            o.put("fps", cam?.measuredFps ?: 0.0)
            o.put("encode_ms", cam?.meanEncodeMs ?: 0.0)
            cam?.outputSize?.let { o.put("resolution", JSONArray(listOf(it.width, it.height))) }
            o.put("fps_target", settings.targetFps)
            o.put("jpeg_quality", settings.jpegQuality)
            o.put("aspect", if (settings.aspect43) "4:3" else "16:9")
            putBattery(o)
            o.put("spool_free_mb", appContext.filesDir.usableSpace / 1_000_000L)
            if (gps.hasFix) o.put("gps_acc", gps.lastAccuracyM.toDouble())
            o.put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
            o.put("android", Build.VERSION.RELEASE)
            o.put("app", BuildConfig.VERSION_NAME)
            // Read back to back: pins the IMU's boot clock to the frames' wall
            // clock on the desktop (the clean fix INTEGRATION.md 4.1 describes).
            o.put("wall_ms", System.currentTimeMillis())
            o.put("elapsed_ns", SystemClock.elapsedRealtimeNanos())
            lastCompleted?.let {
                o.put("last_completed", it)
                o.put("last_frames_sent", lastFramesSent)
            }
            rejected?.let { o.put("rejected", it) }
            lastError?.let { o.put("error", it) }
            client.offerStatus(o.toString())
        } catch (e: Exception) {
            Log.w(TAG, "status not sent", e)
        }
    }

    private fun putBattery(o: JSONObject) {
        val i = ContextCompat.registerReceiver(
            appContext, null, IntentFilter(Intent.ACTION_BATTERY_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED
        ) ?: return
        val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        if (level >= 0 && scale > 0) o.put("battery", level * 100 / scale)
        o.put("charging", i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0)
        val temp = i.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE)
        if (temp != Int.MIN_VALUE) o.put("temp_c", temp / 10.0)
    }

    private fun sendIntrinsics() {
        if (!client.connection.value.remoteControl) return
        val k = computeIntrinsics() ?: return
        client.offerIntrinsics(
            fxPix = k.fx_pix.toFloat(), fyPix = k.fy_pix.toFloat(),
            cxPix = k.cx_pix.toFloat(), cyPix = k.cy_pix.toFloat(),
            k1 = k.k1, k2 = k.k2, p1 = k.p1, p2 = k.p2, k3 = k.k3, source = k.source
        )
        intrinsicsSent = true
    }

    /** Null until the camera has produced at least one frame (outputSize is set then). */
    private fun computeIntrinsics(): CameraIntrinsics? {
        val cam = camera ?: return null
        val buffer = cam.activeResolution ?: return null
        if (cam.outputSize == null) return null
        return try {
            val cm = appContext.getSystemService(Context.CAMERA_SERVICE) as CameraManager
            val id = cm.cameraIdList.firstOrNull {
                cm.getCameraCharacteristics(it)
                    .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
            } ?: cm.cameraIdList.firstOrNull() ?: return null
            cameraIntrinsicsForOutput(cm.getCameraCharacteristics(id), buffer.width, buffer.height, cam.rotationDegrees)
        } catch (e: Exception) {
            Log.w(TAG, "could not extract camera intrinsics", e)
            null
        }
    }

    private fun newSessionId(): String =
        SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())

    // ------------------------------------------------------ local recording

    /**
     * Starts capturing straight to phone storage, no socket involved at all -
     * for a flight with no desktop in reach. Mutually exclusive with a
     * network connection: the camera and sensors have one owner at a time.
     *
     * @return an error string if it could not start, null on success.
     */
    fun startLocalRecording(owner: LifecycleOwner, previewView: PreviewView): String? {
        if (localRecorder != null) return null
        if (_state.value != LinkState.OFF) return "Stop the current connection first"

        val id = newSessionId()
        val dir = File(LocalSessions.rootDir(appContext), id)
        val rec = try {
            LocalSessionRecorder(dir)
        } catch (e: Exception) {
            return "Could not start local recording: ${e.message}"
        }
        localRecorder = rec
        localSessionId = id
        localRecordingStartedAtMs = System.currentTimeMillis()

        if (camera == null) startPreview(owner, previewView)
        camera?.resetCounters()
        camera?.streaming = true
        startSensors(
            imuWanted = true,
            gpsWanted = settings.outdoorMode,
            onImu = { batch -> rec.onImuBatch(batch) },
            onGps = { fix -> rec.onGpsFix(fix) }
        )

        // Camera characteristics are not resolved until the first frame sets
        // outputSize, so this is retried for a few seconds rather than tried once.
        val s = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        localScope = s
        s.launch {
            var sent = false
            while (isActive && !sent) {
                computeIntrinsics()?.let { rec.setIntrinsics(it); sent = true }
                delay(500)
            }
        }

        _localRecording.value = true
        _events.tryEmit("Recording locally: $id")
        StreamingForegroundService.start(
            appContext, needsLocation = settings.outdoorMode, keepAwake = settings.keepAwake
        )
        return null
    }

    /** Stops local recording, writes the session's JSON sidecars, and returns the tally. */
    fun stopLocalRecording(): LocalSessionRecorder.Summary? {
        val rec = localRecorder ?: return null
        camera?.streaming = false
        stopSensors()
        localScope?.cancel()
        localScope = null
        val summary = rec.finish(cameraFps)
        localRecorder = null
        localSessionId = null
        _localRecording.value = false
        _events.tryEmit("Saved locally: ${summary.frames} frames, ${summary.imuSamples} imu, ${summary.gpsFixes} gps")
        StreamingForegroundService.stop(appContext)
        return summary
    }

    // ----------------------------------------------------------- disconnect

    /** DISCONNECT: tears everything down and reports what was moved. */
    fun stop(): Summary {
        if (_state.value == LinkState.OFF) return Summary(0, 0, 0, 0, 0, 0)
        val unsent = client.recordingBacklog
        camera?.streaming = false
        stopSensors()
        // Read the stats *after* stopping: stop() pushes one final snapshot.
        client.stop()
        client.finishRecording()?.close(deleteFile = true)
        if (unsent > 0) Log.w(TAG, "disconnected with $unsent spooled frames unsent")
        scope?.cancel()
        scope = null
        client.onCommand = null
        _state.value = LinkState.OFF
        sessionId = null
        activeParams = null
        val s = client.stats.value
        val summary = Summary(
            durationMs = System.currentTimeMillis() - startedAtMs,
            framesSent = s.framesSent,
            framesDropped = s.framesDropped + unsent,
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

    // ------------------------------------------------------------------ wifi

    private fun connectivityManager() =
        appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    /**
     * True when the active network is WiFi. Deliberately does not require
     * NET_CAPABILITY_INTERNET: a field router or phone hotspot with no uplink
     * is a perfectly good network for reaching a desktop on the same LAN.
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
     * Watches specifically for WiFi. Losing it pauses a live stream; a
     * recording keeps capturing into the spool and uploads when WiFi is back.
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
                    _events.tryEmit(
                        if (_state.value == LinkState.RECORDING) "WiFi lost - still recording to phone storage"
                        else "WiFi disconnected - streaming paused"
                    )
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
