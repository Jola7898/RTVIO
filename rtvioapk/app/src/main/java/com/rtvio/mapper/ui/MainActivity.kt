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
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import com.rtvio.mapper.R
import com.rtvio.mapper.data.SettingsManager
import com.rtvio.mapper.databinding.ActivityMainBinding
import com.rtvio.mapper.data.DeviceSpecsCollector
import com.rtvio.mapper.net.ConnectionState
import com.rtvio.mapper.net.StreamStats
import com.rtvio.mapper.service.StreamingForegroundService
import com.rtvio.mapper.service.StreamingSession
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.Locale

/**
 * The single operational screen: preview, live status, and the start/stop
 * control.
 *
 * All the moving parts live in [StreamingSession]; this class is deliberately
 * limited to permissions, rendering state and relaying user intent.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var settings: SettingsManager
    private lateinit var session: StreamingSession
    private lateinit var specs: DeviceSpecsCollector

    private var statusExpanded = true

    /** Set when the user taps START before the permission dialog resolves. */
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
            beginStreaming()
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
        binding.btnStream.setOnClickListener { onStreamButton() }
        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.statusHeader.setOnClickListener { toggleStatusPanel() }

        // The notification's Stop action and a task swipe both reach the
        // service, which has no handle on the camera or the socket; this is
        // what turns either of those into a real teardown.
        StreamingForegroundService.onStopRequested = {
            runOnUiThread { if (session.isStreaming) endStreaming() }
        }

        observeSession()
        requestStartupPermissions()
    }

    // ----------------------------------------------------------- permissions

    private fun requestStartupPermissions() {
        val wanted = mutableListOf<String>()
        if (!has(Manifest.permission.CAMERA)) wanted += Manifest.permission.CAMERA
        // Location is needed for GPS in outdoor mode, and is also what lets
        // Android report the WiFi SSID on the status screen.
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
        // is the one setting that can be retuned without a rebind, so a running
        // session picks it up too.
        if (session.isStreaming) session.applyQuality() else bindPreviewIfPermitted()
        applyOverlayVisibility()
        renderIdleState()
    }

    override fun onPause() {
        super.onPause()
        // A session in progress keeps the camera; the foreground service is
        // what makes that legal and survivable.
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

    // ----------------------------------------------------------- start / stop

    private fun onStreamButton() {
        binding.btnStream.performHapticFeedbackIfEnabled()
        if (session.isStreaming) endStreaming() else {
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
                        permissionLauncher.launch(
                            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
                        )
                    }
                    .setNegativeButton(R.string.cancel) { _, _ -> beginStreaming() }
                    .show()
                return
            }
            beginStreaming()
        }
    }

    private fun beginStreaming() {
        val error = session.start(this, binding.previewView)
        if (error != null) {
            Snackbar.make(binding.root, error, Snackbar.LENGTH_LONG).show()
            return
        }
        binding.btnStream.setText(R.string.action_stop)
        // MaterialButton manages its own background drawable, so tint it rather
        // than replacing the background outright.
        binding.btnStream.backgroundTintList =
            ColorStateList.valueOf(ContextCompat.getColor(this, R.color.stop_red))
    }

    private fun endStreaming() {
        val summary = session.stop()
        binding.btnStream.setText(R.string.action_start)
        binding.btnStream.backgroundTintList = ColorStateList.valueOf(
            com.google.android.material.color.MaterialColors.getColor(
                binding.btnStream, com.google.android.material.R.attr.colorPrimary
            )
        )
        renderIdleState()
        showSummary(summary)
    }

    private fun showSummary(s: StreamingSession.Summary) {
        val seconds = s.durationMs / 1000.0
        val text = buildString {
            appendLine("Duration: %s".format(formatDuration(s.durationMs)))
            appendLine("Frames sent: %,d".format(s.framesSent))
            appendLine("Frames dropped: %,d".format(s.framesDropped))
            appendLine("IMU samples: %,d".format(s.imuSamples))
            appendLine("GPS fixes: %,d".format(s.gpsFixes))
            appendLine("Data sent: %.1f MB".format(s.bytesSent / 1e6))
            if (seconds > 0) {
                appendLine("Average rate: %.1f fps, %.2f Mbps".format(
                    s.framesSent / seconds, s.bytesSent * 8 / 1e6 / seconds
                ))
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
                        val (label, color) = when (info.state) {
                            ConnectionState.CONNECTED ->
                                getString(R.string.state_connected) to R.color.status_ok
                            ConnectionState.CONNECTING ->
                                getString(R.string.state_connecting) to R.color.status_warn
                            ConnectionState.ERROR ->
                                getString(R.string.state_error) to R.color.status_error
                            ConnectionState.DISCONNECTED ->
                                getString(R.string.state_disconnected) to R.color.status_error
                        }
                        binding.statusState.text = label
                        binding.statusState.setTextColor(ContextCompat.getColor(this@MainActivity, color))
                        binding.statusDetail.text = info.detail
                        binding.statusDetail.visibility =
                            if (info.detail.isEmpty()) View.GONE else View.VISIBLE
                        binding.rowServer.rowValue.text =
                            if (info.host.isEmpty()) getString(R.string.no_server_set)
                            else "${info.host}:${info.port}"
                    }
                }
                launch {
                    session.client.stats.collectLatest { renderStats(it) }
                }
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
                // Camera FPS, GPS age, gyro and battery are polled rather than
                // pushed: they change continuously, and a 1 Hz refresh is all a
                // human can read anyway.
                launch { pollFastState() }
            }
        }
    }

    private suspend fun pollFastState() {
        while (currentCoroutineContext().isActive) {
            binding.rowFps.rowValue.text = "%.1f".format(session.cameraFps)
            updateOverlays()
            updateBattery()
            updateModeRow()
            if (session.isStreaming) {
                StreamingForegroundService.update(
                    this,
                    "%.0f fps  |  %s".format(
                        session.cameraFps, binding.rowBandwidth.rowValue.text
                    )
                )
            }
            delay(1000)
        }
    }

    private fun renderStats(s: StreamStats) {
        binding.rowLatency.rowValue.text = "%.0f ms".format(s.latencyMs)
        binding.rowBandwidth.rowValue.text = "%.2f Mbps".format(s.mbps)
        binding.rowBuffer.rowValue.text =
            "${s.videoQueueDepth}/${s.videoQueueCapacity}  (-${s.framesDropped})"
        binding.rowFrames.rowValue.text = "%,d".format(s.framesSent)
        binding.rowImu.rowValue.text = "%,d".format(s.imuSamplesSent)
    }

    private fun renderIdleState() {
        val ip = settings.serverIp
        binding.rowServer.rowValue.text =
            if (ip.isEmpty()) getString(R.string.no_server_set) else "$ip:${settings.serverPort}"
        updateModeRow()
        updateBattery()
    }

    private fun updateModeRow() {
        val mode = if (settings.outdoorMode) getString(R.string.mode_outdoor)
        else getString(R.string.mode_indoor)
        val gpsPart = when {
            !settings.outdoorMode -> ""
            !session.gps.isProviderEnabled -> " (GPS off)"
            session.gps.hasFix -> " (%.0f m)".format(session.gps.lastAccuracyM)
            session.isStreaming -> " (no fix)"
            else -> ""
        }
        binding.rowMode.rowValue.text = mode + gpsPart
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
            binding.overlayFps.text = "%.1f fps".format(session.cameraFps)
            val res = session.activeResolution
            if (res != null) {
                binding.overlayFps.text = "%.1f fps  %dx%d".format(
                    session.cameraFps, res.width, res.height
                )
            }
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
