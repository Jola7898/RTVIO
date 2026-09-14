package com.rtvio.mapper.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings as AndroidSettings
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.color.MaterialColors
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import com.rtvio.mapper.R
import com.rtvio.mapper.data.DeviceSpecsCollector
import com.rtvio.mapper.data.SettingsManager
import com.rtvio.mapper.databinding.ActivityMainBinding
import com.rtvio.mapper.net.ConnectionState
import com.rtvio.mapper.net.StreamStats
import com.rtvio.mapper.service.LinkState
import com.rtvio.mapper.service.StreamingForegroundService
import com.rtvio.mapper.service.StreamingSession
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.Locale

/**
 * The single operational screen: preview, live status, and the controls.
 *
 * CONNECT opens the link. Against RTVIO Studio (the desktop web UI) the phone
 * then sits READY with the viewfinder going to the desktop, and recordings are
 * started and stopped from the browser - or with the REC button here. Against
 * an older receiver it streams live, as it always did.
 *
 * All the moving parts live in [StreamingSession]; this class is limited to
 * permissions, rendering state and relaying user intent.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var settings: SettingsManager
    private lateinit var session: StreamingSession
    private lateinit var specs: DeviceSpecsCollector

    private var statusExpanded = true

    /** Set when the user taps CONNECT before the permission dialog resolves. */
    private var startAfterPermission = false

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        if (granted[Manifest.permission.CAMERA] == false) {
            startAfterPermission = false
            showCameraDenied()
            return@registerForActivityResult
        }
        bindPreviewIfPermitted()
        if (startAfterPermission) {
            startAfterPermission = false
            connect()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        settings = SettingsManager(this)
        session = StreamingSession(applicationContext, settings)
        specs = DeviceSpecsCollector(this)

        binding.toolbar.inflateMenu(R.menu.main_menu)
        binding.toolbar.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.menu_specs -> {
                    startActivity(Intent(this, PhoneSpecsActivity::class.java)); true
                }
                R.id.menu_settings -> {
                    startActivity(Intent(this, SettingsActivity::class.java)); true
                }
                else -> false
            }
        }

        labelRows()
        binding.btnStream.setOnClickListener { onConnectButton() }
        binding.btnRecord.setOnClickListener {
            it.performHapticFeedbackIfEnabled()
            session.toggleRecording()
        }
        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.statusHeader.setOnClickListener { toggleStatusPanel() }

        // The notification's Stop action and a task swipe both reach the
        // service, which has no handle on the camera or the socket; this is
        // what turns either of those into a real teardown.
        StreamingForegroundService.onStopRequested = {
            runOnUiThread { if (session.isStreaming) endSession() }
        }

        observeSession()
        requestStartupPermissions()
    }

    // ----------------------------------------------------------- permissions

    private fun requestStartupPermissions() {
        val wanted = mutableListOf<String>()
        if (!has(Manifest.permission.CAMERA)) wanted += Manifest.permission.CAMERA
        // Location is needed for GPS in outdoor mode (or when the desktop asks
        // for GPS), and is also what lets Android report the WiFi SSID.
        if (settings.outdoorMode && !has(Manifest.permission.ACCESS_FINE_LOCATION)) {
            wanted += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            !has(Manifest.permission.POST_NOTIFICATIONS)
        ) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        if (wanted.isEmpty()) {
            bindPreviewIfPermitted()
        } else {
            permissionLauncher.launch(wanted.toTypedArray())
        }
    }

    private fun has(permission: String) =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun showCameraDenied() {
        Snackbar.make(binding.root, R.string.perm_denied_camera, Snackbar.LENGTH_INDEFINITE)
            .setAction(R.string.perm_open_settings) {
                startActivity(
                    Intent(
                        AndroidSettings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null)
                    )
                )
            }
            .show()
    }

    // -------------------------------------------------------------- lifecycle

    override fun onResume() {
        super.onResume()
        // Rebinding here picks up any resolution or frame-rate change made in
        // Settings, but never mid-session: rebinding would drop frames. Quality
        // is the one setting that can be retuned without a rebind.
        if (session.isStreaming) session.applyQuality() else bindPreviewIfPermitted()
        applyOverlayVisibility()
        renderIdleState()
    }

    override fun onPause() {
        super.onPause()
        // A connected session keeps the camera; the foreground service is what
        // makes that legal and survivable.
        if (!session.isStreaming) session.stopPreview()
    }

    override fun onDestroy() {
        StreamingForegroundService.onStopRequested = null
        if (session.isStreaming) session.stop()
        session.stopPreview()
        super.onDestroy()
    }

    private fun bindPreviewIfPermitted() {
        if (!has(Manifest.permission.CAMERA)) return
        session.startPreview(this, binding.previewView)
    }

    // ---------------------------------------------------------- connect/stop

    private fun onConnectButton() {
        binding.btnStream.performHapticFeedbackIfEnabled()
        if (session.isStreaming) {
            disconnect()
            return
        }
        if (!has(Manifest.permission.CAMERA)) {
            startAfterPermission = true
            permissionLauncher.launch(arrayOf(Manifest.permission.CAMERA))
            return
        }
        if (settings.outdoorMode && !has(Manifest.permission.ACCESS_FINE_LOCATION)) {
            // Outdoor mode without location still streams video and IMU, so
            // ask, explain, and let the session proceed either way.
            MaterialAlertDialogBuilder(this)
                .setMessage(R.string.perm_location_rationale)
                .setPositiveButton(R.string.perm_grant) { _, _ ->
                    startAfterPermission = true
                    permissionLauncher.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION))
                }
                .setNegativeButton(R.string.cancel) { _, _ -> connect() }
                .show()
            return
        }
        connect()
    }

    private fun connect() {
        val error = session.start(this, binding.previewView)
        if (error != null) Snackbar.make(binding.root, error, Snackbar.LENGTH_LONG).show()
    }

    /** Asks before throwing away recorded frames that have not been uploaded. */
    private fun disconnect() {
        val backlog = session.recordingBacklog
        val state = session.state.value
        if (state == LinkState.RECORDING || backlog > 0) {
            MaterialAlertDialogBuilder(this)
                .setTitle(R.string.disconnect_unsent_title)
                .setMessage(getString(R.string.disconnect_unsent_message, maxOf(backlog, 1)))
                .setPositiveButton(R.string.disconnect_anyway) { _, _ -> endSession() }
                .setNegativeButton(R.string.cancel, null)
                .show()
            return
        }
        endSession()
    }

    private fun endSession() {
        val summary = session.stop()
        renderIdleState()
        showSummary(summary)
    }

    private fun showSummary(s: StreamingSession.Summary) {
        val seconds = s.durationMs / 1000.0
        val text = buildString {
            appendLine("Connected: %s".format(formatDuration(s.durationMs)))
            appendLine("Frames sent: %,d".format(s.framesSent))
            appendLine("Frames dropped: %,d".format(s.framesDropped))
            appendLine("IMU samples: %,d".format(s.imuSamples))
            appendLine("GPS fixes: %,d".format(s.gpsFixes))
            appendLine("Data sent: %.1f MB".format(s.bytesSent / 1e6))
            if (seconds > 0) {
                appendLine("Average: %.2f Mbps".format(s.bytesSent * 8 / 1e6 / seconds))
            }
        }
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.summary_title)
            .setMessage(text.trim())
            .setPositiveButton(R.string.ok, null)
            .show()
    }

    // ------------------------------------------------------------- rendering

    private fun labelRows() {
        binding.rowServer.rowLabel.setText(R.string.label_server)
        binding.rowFps.rowLabel.setText(R.string.label_fps)
        binding.rowLatency.rowLabel.setText(R.string.label_latency)
        binding.rowBandwidth.rowLabel.setText(R.string.label_bandwidth)
        binding.rowBuffer.rowLabel.setText(R.string.label_buffer)
        binding.rowMode.rowLabel.setText(R.string.label_mode)
        binding.rowFrames.rowLabel.setText(R.string.label_frames)
        binding.rowImu.rowLabel.setText(R.string.label_imu)
        binding.rowBattery.rowLabel.setText(R.string.label_battery)
    }

    private fun observeSession() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    session.client.connection.collectLatest { info ->
                        binding.statusDetail.text = info.detail
                        binding.statusDetail.visibility =
                            if (info.detail.isEmpty()) View.GONE else View.VISIBLE
                        binding.rowServer.rowValue.text =
                            if (info.host.isEmpty()) getString(R.string.no_server_set)
                            else "${info.host}:${info.port}"
                        renderState()
                    }
                }
                launch { session.state.collectLatest { renderState() } }
                launch { session.client.stats.collectLatest { renderStats(it) } }
                launch {
                    session.events.collectLatest {
                        Snackbar.make(binding.root, it, Snackbar.LENGTH_LONG).show()
                    }
                }
                launch {
                    session.wifiConnected.collectLatest { up ->
                        binding.overlayWifiLost.visibility =
                            if (up || !session.isStreaming) View.GONE else View.VISIBLE
                    }
                }
                // Camera FPS, recording time, GPS age, gyro and battery are
                // polled: they change continuously, and a human reads 1 Hz.
                launch { pollFastState() }
            }
        }
    }

    /** Buttons, header label and screen-on flag from the session state. */
    private fun renderState() {
        val state = session.state.value
        val conn = session.client.connection.value

        binding.btnStream.setText(
            when (state) {
                LinkState.OFF -> R.string.action_connect
                LinkState.CONNECTING -> R.string.action_cancel_connect
                LinkState.STREAMING -> R.string.action_stop
                else -> R.string.action_disconnect
            }
        )
        binding.btnStream.backgroundTintList = ColorStateList.valueOf(
            when (state) {
                LinkState.OFF -> MaterialColors.getColor(binding.btnStream, com.google.android.material.R.attr.colorPrimary)
                LinkState.STREAMING -> ContextCompat.getColor(this, R.color.stop_red)
                else -> MaterialColors.getColor(binding.btnStream, com.google.android.material.R.attr.colorSecondary)
            }
        )

        val remote = state == LinkState.ARMED || state == LinkState.RECORDING || state == LinkState.FINISHING
        binding.btnRecord.visibility = if (remote) View.VISIBLE else View.GONE
        binding.btnRecord.isEnabled = state != LinkState.FINISHING
        binding.btnRecord.text = when (state) {
            LinkState.RECORDING -> getString(R.string.action_stop_record)
            LinkState.FINISHING -> getString(R.string.action_uploading, session.recordingBacklog)
            else -> getString(R.string.action_record)
        }

        val (label, color) = when {
            state == LinkState.RECORDING -> getString(R.string.state_recording) to R.color.status_error
            state == LinkState.FINISHING -> getString(R.string.state_finishing) to R.color.status_warn
            state == LinkState.ARMED -> getString(R.string.state_armed) to R.color.status_ok
            conn.state == ConnectionState.CONNECTED -> getString(R.string.state_connected) to R.color.status_ok
            conn.state == ConnectionState.CONNECTING -> getString(R.string.state_connecting) to R.color.status_warn
            conn.state == ConnectionState.ERROR -> getString(R.string.state_error) to R.color.status_error
            else -> getString(R.string.state_disconnected) to R.color.status_error
        }
        binding.statusState.text = label
        binding.statusState.setTextColor(ContextCompat.getColor(this, color))

        // A timeout blanking the screen would stop CameraX (it is bound to
        // this activity's lifecycle) in the middle of a take.
        if (state != LinkState.OFF) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        updateRecOverlay()
        updateModeRow()
    }

    private suspend fun pollFastState() {
        while (currentCoroutineContext().isActive) {
            binding.rowFps.rowValue.text = "%.1f".format(session.cameraFps)
            updateOverlays()
            updateRecOverlay()
            updateBattery()
            updateModeRow()
            if (session.state.value == LinkState.FINISHING) {
                binding.btnRecord.text = getString(R.string.action_uploading, session.recordingBacklog)
            }
            if (session.isStreaming) {
                StreamingForegroundService.update(
                    this,
                    when (session.state.value) {
                        LinkState.RECORDING -> "● REC %s  |  %.0f fps".format(recElapsed(), session.cameraFps)
                        LinkState.FINISHING -> "Uploading %d frames".format(session.recordingBacklog)
                        LinkState.ARMED -> "Ready - waiting for the desktop"
                        else -> "%.0f fps  |  %s".format(session.cameraFps, binding.rowBandwidth.rowValue.text)
                    }
                )
            }
            delay(1000)
        }
    }

    private fun recElapsed(): String {
        val start = session.recordingStartedAtMs
        return if (start == 0L) "00:00" else formatDuration(System.currentTimeMillis() - start).substring(3)
    }

    private fun updateRecOverlay() {
        val o = binding.overlayRec
        when (session.state.value) {
            LinkState.RECORDING -> {
                val backlog = session.recordingBacklog
                o.text = buildString {
                    append("● REC ").append(recElapsed())
                    append("  ·  %,d".format(session.framesCaptured))
                    if (backlog > 30) append("  ·  %,d queued".format(backlog))
                }
                o.visibility = View.VISIBLE
            }
            LinkState.FINISHING -> {
                o.text = getString(R.string.action_uploading, session.recordingBacklog)
                o.visibility = View.VISIBLE
            }
            else -> o.visibility = View.GONE
        }
    }

    private fun renderStats(s: StreamStats) {
        binding.rowLatency.rowValue.text = "%.0f ms".format(s.latencyMs)
        binding.rowBandwidth.rowValue.text = "%.2f Mbps".format(s.mbps)
        binding.rowBuffer.rowValue.text =
            if (s.spoolPending > 0 || session.state.value == LinkState.RECORDING)
                "spool %,d  (%.0f MB)".format(s.spoolPending, s.spoolBytes / 1e6)
            else "${s.videoQueueDepth}/${s.videoQueueCapacity}  (-${s.framesDropped})"
        binding.rowFrames.rowValue.text = "%,d".format(s.framesSent)
        binding.rowImu.rowValue.text = "%,d".format(s.imuSamplesSent)
    }

    private fun renderIdleState() {
        val ip = settings.serverIp
        binding.rowServer.rowValue.text =
            if (ip.isEmpty()) getString(R.string.no_server_set) else "$ip:${settings.serverPort}"
        renderState()
        updateBattery()
    }

    private fun updateModeRow() {
        val text = when (session.state.value) {
            LinkState.ARMED -> getString(R.string.mode_remote_armed)
            LinkState.RECORDING -> getString(R.string.mode_remote_recording, session.sessionId ?: "")
            LinkState.FINISHING -> getString(R.string.mode_remote_finishing, session.recordingBacklog)
            LinkState.STREAMING -> getString(R.string.mode_live) + gpsPart()
            else -> (if (settings.outdoorMode) getString(R.string.mode_outdoor)
                     else getString(R.string.mode_indoor)) + gpsPart()
        }
        binding.rowMode.rowValue.text = text
    }

    private fun gpsPart(): String = when {
        !settings.outdoorMode -> ""
        !session.gps.isProviderEnabled -> " (GPS off)"
        session.gps.hasFix -> " (%.0f m)".format(session.gps.lastAccuracyM)
        session.isStreaming -> " (no fix)"
        else -> ""
    }

    private fun updateBattery() {
        val b = specs.batterySnapshot()
        binding.rowBattery.rowValue.text = when {
            b.percent < 0 -> "-"
            b.charging -> "${b.percent}% chg"
            else -> "${b.percent}%"
        }
    }

    private fun updateOverlays() {
        if (settings.showFpsOverlay) {
            val res = session.activeResolution
            binding.overlayFps.text = if (res != null)
                "%.1f fps  %dx%d".format(session.cameraFps, res.width, res.height)
            else "%.1f fps".format(session.cameraFps)
        }
        if (settings.outdoorMode) {
            val age = session.gps.fixAgeMs
            binding.overlayGps.text = when {
                !session.gps.isProviderEnabled -> "GPS off"
                age < 0 -> "GPS no fix"
                age > 10_000 -> "GPS %ds old".format(age / 1000)
                else -> "GPS %.0f m".format(session.gps.lastAccuracyM)
            }
        }
        // Angular rate in deg/s. Above roughly 60 deg/s a 1/30 s exposure smears
        // detail across enough pixels to start costing feature matches.
        val degPerSec = Math.toDegrees(session.gyroMagnitude.toDouble())
        binding.overlayStability.text = when {
            degPerSec > 90 -> "⚠ too fast  %.0f°/s".format(degPerSec)
            degPerSec > 45 -> "moving  %.0f°/s".format(degPerSec)
            else -> "steady  %.0f°/s".format(degPerSec)
        }
    }

    private fun applyOverlayVisibility() {
        binding.overlayFps.visibility = if (settings.showFpsOverlay) View.VISIBLE else View.GONE
        binding.overlayGps.visibility =
            if (settings.showFpsOverlay && settings.outdoorMode) View.VISIBLE else View.GONE
        binding.overlayStability.visibility =
            if (settings.showFpsOverlay) View.VISIBLE else View.GONE
        binding.statusBody.visibility = if (statusExpanded) View.VISIBLE else View.GONE
        listOf(
            binding.rowLatency, binding.rowBandwidth, binding.rowBuffer
        ).forEach { it.root.visibility = if (settings.showStats) View.VISIBLE else View.GONE }
    }

    private fun toggleStatusPanel() {
        statusExpanded = !statusExpanded
        binding.statusBody.visibility = if (statusExpanded) View.VISIBLE else View.GONE
        binding.statusChevron.text = if (statusExpanded) "▾" else "▸"
    }

    private fun View.performHapticFeedbackIfEnabled() {
        if (settings.haptics) performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
    }

    private fun formatDuration(ms: Long): String {
        val total = ms / 1000
        return String.format(Locale.US, "%02d:%02d:%02d", total / 3600, (total % 3600) / 60, total % 60)
    }
}
