package com.rtvio.mapper.data

import android.Manifest
import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.hardware.Sensor
import android.hardware.SensorManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.params.StreamConfigurationMap
import android.location.LocationManager
import android.net.wifi.ScanResult
import android.net.wifi.WifiInfo
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.util.Log
import android.util.Size
import androidx.core.content.ContextCompat
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.atan
import kotlin.math.roundToInt
import kotlin.math.sqrt
import kotlin.math.tan

/**
 * Reads everything the phone will tell us about itself.
 *
 * Every probe is individually guarded: a device that hides its WiFi SSID, has
 * no gyroscope, or refuses camera characteristics must still produce a complete
 * specs screen with the rest filled in. Nothing here throws.
 */
class DeviceSpecsCollector(private val context: Context) {

    private companion object {
        const val TAG = "DeviceSpecs"
        const val UNKNOWN = "unavailable"
    }

    fun collectAll(): List<SpecSection> = listOf(
        deviceSection(),
        cpuSection(),
        memorySection(),
        cameraSection(),
        imuSection(),
        gpsSection(),
        wifiSection(),
        batterySection(),
        screenSection()
    )

    // ---------------------------------------------------------------- device

    private fun deviceSection() = SpecSection(
        "Device", listOf(
            SpecItem("Manufacturer", Build.MANUFACTURER),
            SpecItem("Model", Build.MODEL),
            SpecItem("Device", Build.DEVICE),
            SpecItem("Brand", Build.BRAND),
            SpecItem("Product", Build.PRODUCT),
            SpecItem("Android", "${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})"),
            SpecItem("Build ID", Build.ID)
        )
    )

    // ------------------------------------------------------------------- cpu

    private fun cpuSection(): SpecSection {
        val items = mutableListOf(
            SpecItem("Cores (available)", Runtime.getRuntime().availableProcessors().toString()),
            SpecItem("Primary ABI", Build.SUPPORTED_ABIS.firstOrNull() ?: UNKNOWN),
            SpecItem("All ABIs", Build.SUPPORTED_ABIS.joinToString(", "))
        )
        cpuModelFromProc()?.let { items.add(SpecItem("CPU", it)) }
        maxCpuFrequencyMhz()?.let { items.add(SpecItem("Max frequency", "$it MHz")) }
        return SpecSection("CPU", items)
    }

    /** /proc/cpuinfo is world-readable and still the only source for a model string. */
    private fun cpuModelFromProc(): String? = try {
        File("/proc/cpuinfo").useLines { lines ->
            lines.firstOrNull { it.startsWith("Hardware") || it.startsWith("model name") }
                ?.substringAfter(':')
                ?.trim()
                ?.takeIf { it.isNotEmpty() }
        }
    } catch (e: Exception) {
        null
    }

    private fun maxCpuFrequencyMhz(): Int? = try {
        (0 until Runtime.getRuntime().availableProcessors()).mapNotNull { core ->
            File("/sys/devices/system/cpu/cpu$core/cpufreq/cpuinfo_max_freq")
                .takeIf { it.canRead() }
                ?.readText()
                ?.trim()
                ?.toLongOrNull()
        }.maxOrNull()?.let { (it / 1000).toInt() }
    } catch (e: Exception) {
        null
    }

    // ---------------------------------------------------------------- memory

    private fun memorySection(): SpecSection {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val mi = ActivityManager.MemoryInfo()
        am.getMemoryInfo(mi)
        return SpecSection(
            "Memory", listOf(
                SpecItem("Total RAM", formatBytes(mi.totalMem)),
                SpecItem("Available RAM", formatBytes(mi.availMem)),
                SpecItem("Low-memory threshold", formatBytes(mi.threshold)),
                SpecItem("Under memory pressure", if (mi.lowMemory) "yes" else "no"),
                SpecItem("Per-app heap limit", "${am.memoryClass} MB"),
                SpecItem("Large heap limit", "${am.largeMemoryClass} MB")
            )
        )
    }

    // ---------------------------------------------------------------- camera

    /** Structured camera facts for the back-facing camera, or null if unreadable. */
    fun primaryCameraSpecs(): CameraSpecs? {
        val cm = context.getSystemService(Context.CAMERA_SERVICE) as? CameraManager ?: return null
        return try {
            val id = cm.cameraIdList.firstOrNull { camId ->
                cm.getCameraCharacteristics(camId)
                    .get(CameraCharacteristics.LENS_FACING) == CameraMetadata.LENS_FACING_BACK
            } ?: cm.cameraIdList.firstOrNull() ?: return null
            describeCamera(cm, id)
        } catch (e: Exception) {
            Log.w(TAG, "camera characteristics unavailable", e)
            null
        }
    }

    private fun describeCamera(cm: CameraManager, id: String): CameraSpecs {
        val ch = cm.getCameraCharacteristics(id)
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)

        val maxJpeg = map?.getOutputSizes(ImageFormat.JPEG)
            ?.maxByOrNull { it.width.toLong() * it.height }

        val focal = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS) ?: FloatArray(0)
        val sensor = ch.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
        val sw = sensor?.width ?: 0f
        val sh = sensor?.height ?: 0f

        // Thin-lens FOV: 2 * atan(sensorDimension / (2 * focalLength)).
        val f = focal.firstOrNull() ?: 0f
        val hFov = if (f > 0f && sw > 0f) {
            Math.toDegrees(2.0 * atan((sw / (2 * f)).toDouble()))
        } else 0.0
        val vFov = if (f > 0f && sh > 0f) {
            Math.toDegrees(2.0 * atan((sh / (2 * f)).toDouble()))
        } else 0.0

        return CameraSpecs(
            cameraId = id,
            lensFacing = when (ch.get(CameraCharacteristics.LENS_FACING)) {
                CameraMetadata.LENS_FACING_BACK -> "back"
                CameraMetadata.LENS_FACING_FRONT -> "front"
                CameraMetadata.LENS_FACING_EXTERNAL -> "external"
                else -> UNKNOWN
            },
            maxJpegSize = maxJpeg,
            focalLengthsMm = focal,
            sensorWidthMm = sw,
            sensorHeightMm = sh,
            horizontalFovDeg = hFov,
            verticalFovDeg = vFov,
            maxFpsAtDefaultSize = maxFpsFor(map, maxJpeg),
            supportedFormats = describeFormats(map),
            hardwareLevel = when (ch.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)) {
                CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
                CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
                CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
                CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
                else -> UNKNOWN
            }
        )
    }

    /** Minimum frame duration is nanoseconds per frame; its reciprocal is the ceiling FPS. */
    private fun maxFpsFor(map: StreamConfigurationMap?, size: Size?): Double {
        if (map == null || size == null) return 0.0
        return try {
            val minDurationNs = map.getOutputMinFrameDuration(ImageFormat.JPEG, size)
            if (minDurationNs > 0) 1_000_000_000.0 / minDurationNs else 0.0
        } catch (e: Exception) {
            0.0
        }
    }

    private fun describeFormats(map: StreamConfigurationMap?): List<String> {
        val known = mapOf(
            ImageFormat.JPEG to "JPEG",
            ImageFormat.YUV_420_888 to "YUV_420_888",
            ImageFormat.PRIVATE to "PRIVATE",
            ImageFormat.RAW_SENSOR to "RAW_SENSOR",
            ImageFormat.NV21 to "NV21",
            ImageFormat.YV12 to "YV12"
        )
        return map?.outputFormats?.map { known[it] ?: "0x" + Integer.toHexString(it) } ?: emptyList()
    }

    private fun cameraSection(): SpecSection {
        val c = primaryCameraSpecs()
            ?: return SpecSection("Camera", listOf(SpecItem("Status", UNKNOWN)))
        val res = c.maxJpegSize
        val megapixels = res?.let { it.width.toDouble() * it.height / 1_000_000.0 }
        return SpecSection(
            "Camera (primary)", listOfNotNull(
                SpecItem("Camera ID", c.cameraId + " (" + c.lensFacing + ")"),
                SpecItem("Hardware level", c.hardwareLevel),
                res?.let {
                    val mp = megapixels?.let { m -> "  (%.1f MP)".format(m) } ?: ""
                    SpecItem("Max resolution", "${it.width} x ${it.height}$mp")
                },
                SpecItem(
                    "Focal length",
                    if (c.focalLengthsMm.isEmpty()) UNKNOWN
                    else c.focalLengthsMm.joinToString(", ") { "%.2f mm".format(it) }
                ),
                SpecItem(
                    "Sensor physical size",
                    if (c.sensorWidthMm > 0) "%.2f x %.2f mm".format(c.sensorWidthMm, c.sensorHeightMm)
                    else UNKNOWN
                ),
                SpecItem(
                    "Field of view",
                    if (c.horizontalFovDeg > 0) {
                        "%.1f deg H / %.1f deg V / %.1f deg diagonal".format(
                            c.horizontalFovDeg,
                            c.verticalFovDeg,
                            diagonalFov(c.horizontalFovDeg, c.verticalFovDeg)
                        )
                    } else UNKNOWN
                ),
                SpecItem(
                    "Max FPS at max resolution",
                    if (c.maxFpsAtDefaultSize > 0) "%.1f fps".format(c.maxFpsAtDefaultSize) else UNKNOWN
                ),
                SpecItem(
                    "Output formats",
                    c.supportedFormats.joinToString(", ").ifEmpty { UNKNOWN }
                )
            )
        )
    }

    /** Diagonal FOV from the two axis FOVs, via the equivalent focal length. */
    private fun diagonalFov(hDeg: Double, vDeg: Double): Double {
        val th = tan(Math.toRadians(hDeg) / 2)
        val tv = tan(Math.toRadians(vDeg) / 2)
        return Math.toDegrees(2 * atan(sqrt(th * th + tv * tv)))
    }

    // ------------------------------------------------------------------- imu

    private fun imuSection(): SpecSection {
        val sm = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
        val items = mutableListOf<SpecItem>()
        items += describeSensor(sm, Sensor.TYPE_ACCELEROMETER, "Accelerometer", "m/s^2")
        items += describeSensor(sm, Sensor.TYPE_GYROSCOPE, "Gyroscope", "rad/s")
        items += describeSensor(sm, Sensor.TYPE_MAGNETIC_FIELD, "Magnetometer", "uT")
        return SpecSection("Inertial sensors", items)
    }

    private fun describeSensor(
        sm: SensorManager,
        type: Int,
        name: String,
        unit: String
    ): List<SpecItem> {
        val s = sm.getDefaultSensor(type) ?: return listOf(SpecItem(name, "not present"))
        // minDelay is the shortest inter-sample interval in microseconds; its
        // reciprocal is the fastest rate the sensor will deliver. A minDelay of
        // 0 means the sensor reports on change, not continuously.
        val maxRate = if (s.minDelay > 0) 1_000_000.0 / s.minDelay else 0.0
        return listOf(
            SpecItem("$name - vendor", s.vendor + " " + s.name),
            SpecItem("$name - full-scale range", "+/- %.3f %s".format(s.maximumRange, unit)),
            SpecItem("$name - resolution", "%.3e %s / LSB".format(s.resolution, unit)),
            SpecItem(
                "$name - max rate",
                if (maxRate > 0) "%.0f Hz (minDelay %d us)".format(maxRate, s.minDelay)
                else "on-change"
            ),
            SpecItem("$name - power", "%.2f mA".format(s.power))
        )
    }

    // ------------------------------------------------------------------- gps

    private fun gpsSection(): SpecSection {
        val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val items = mutableListOf<SpecItem>()
        val enabled = try {
            lm.isProviderEnabled(LocationManager.GPS_PROVIDER)
        } catch (e: Exception) {
            false
        }
        items += SpecItem("GPS provider", if (enabled) "enabled" else "disabled")
        items += SpecItem(
            "Location permission",
            if (hasLocationPermission()) "granted" else "not granted"
        )

        if (hasLocationPermission()) {
            try {
                @Suppress("MissingPermission")
                val fix = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER)
                if (fix != null) {
                    items += SpecItem("Last fix accuracy", "%.1f m".format(fix.accuracy))
                    items += SpecItem("Last fix altitude", "%.1f m".format(fix.altitude))
                    val ageSec = (System.currentTimeMillis() - fix.time) / 1000
                    items += SpecItem("Last fix age", "$ageSec s")
                } else {
                    items += SpecItem("Last fix", "none cached")
                }
            } catch (e: SecurityException) {
                items += SpecItem("Last fix", UNKNOWN)
            }
        }
        return SpecSection("GPS", items)
    }

    private fun hasLocationPermission() = ContextCompat.checkSelfPermission(
        context, Manifest.permission.ACCESS_FINE_LOCATION
    ) == PackageManager.PERMISSION_GRANTED

    // ------------------------------------------------------------------ wifi

    private fun wifiSection(): SpecSection {
        val items = mutableListOf<SpecItem>()
        try {
            val wm = context.applicationContext
                .getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            val info: WifiInfo? = wm.connectionInfo
            if (info == null || (info.networkId == -1 && info.ssid.isNullOrEmpty())) {
                items += SpecItem("Status", "not connected")
            } else {
                // SSID comes back quoted, and reads as <unknown ssid> unless the
                // app holds ACCESS_FINE_LOCATION on API 27+.
                val ssid = info.ssid?.trim('"').orEmpty()
                items += SpecItem(
                    "SSID",
                    if (ssid.isEmpty() || ssid == "<unknown ssid>") {
                        "hidden (needs location permission)"
                    } else ssid
                )
                items += SpecItem(
                    "Signal strength",
                    "${info.rssi} dBm (" + signalLabel(info.rssi) + ")"
                )
                items += SpecItem("Negotiated link speed", "${info.linkSpeed} Mbps")
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    items += SpecItem(
                        "Frequency",
                        "${info.frequency} MHz (" + bandLabel(info.frequency) + ")"
                    )
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    items += SpecItem("WiFi standard", wifiStandardLabel(info.wifiStandard))
                    items += SpecItem("Downlink estimate", "${info.rxLinkSpeedMbps} Mbps")
                    items += SpecItem("Uplink estimate", "${info.txLinkSpeedMbps} Mbps")
                }
                // The uplink is what matters here; we only ever send.
                items += SpecItem("Usable uplink (est.)", "%.1f Mbps".format(usableUplinkMbps(info)))
            }
        } catch (e: Exception) {
            Log.w(TAG, "wifi info unavailable", e)
            items += SpecItem("Status", UNKNOWN)
        }
        return SpecSection("WiFi", items)
    }

    /**
     * Real TCP throughput on WiFi lands well under the negotiated PHY rate:
     * 802.11 framing overhead, retries and half-duplex media access typically
     * leave 40-60% of it. 50% is the planning number used here.
     */
    private fun usableUplinkMbps(info: WifiInfo): Double {
        val phy = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && info.txLinkSpeedMbps > 0) {
            info.txLinkSpeedMbps.toDouble()
        } else {
            info.linkSpeed.toDouble()
        }
        return phy.coerceAtLeast(0.0) * 0.5
    }

    private fun signalLabel(rssi: Int) = when {
        rssi >= -50 -> "excellent"
        rssi >= -60 -> "good"
        rssi >= -70 -> "fair"
        else -> "weak"
    }

    private fun bandLabel(freqMhz: Int) = when {
        freqMhz >= 5925 -> "6 GHz"
        freqMhz >= 4900 -> "5 GHz"
        else -> "2.4 GHz"
    }

    /**
     * WifiInfo.getWifiStandard() returns one of these values, but the constants
     * themselves are declared on ScanResult, not on WifiInfo.
     */
    private fun wifiStandardLabel(standard: Int): String {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            standard == ScanResult.WIFI_STANDARD_11BE
        ) {
            return "802.11be (WiFi 7)"
        }
        return when (standard) {
            ScanResult.WIFI_STANDARD_LEGACY -> "802.11 a/b/g"
            ScanResult.WIFI_STANDARD_11N -> "802.11n (WiFi 4)"
            ScanResult.WIFI_STANDARD_11AC -> "802.11ac (WiFi 5)"
            ScanResult.WIFI_STANDARD_11AX -> "802.11ax (WiFi 6)"
            else -> UNKNOWN
        }
    }

    // --------------------------------------------------------------- battery

    /** Live battery snapshot: percent, charging, temperature in Celsius. */
    fun batterySnapshot(): BatteryState {
        val intent: Intent? = context.registerReceiver(
            null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)
        )
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val pct = if (level >= 0 && scale > 0) (level * 100f / scale).roundToInt() else -1
        val status = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
        // EXTRA_TEMPERATURE is in tenths of a degree Celsius.
        val tempC = (intent?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1) ?: -1) / 10.0
        return BatteryState(pct, charging, tempC)
    }

    private fun batterySection(): SpecSection {
        val b = batterySnapshot()
        val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val healthLabel = when (intent?.getIntExtra(BatteryManager.EXTRA_HEALTH, -1)) {
            BatteryManager.BATTERY_HEALTH_GOOD -> "good"
            BatteryManager.BATTERY_HEALTH_OVERHEAT -> "overheating"
            BatteryManager.BATTERY_HEALTH_DEAD -> "dead"
            BatteryManager.BATTERY_HEALTH_COLD -> "cold"
            BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "over voltage"
            else -> UNKNOWN
        }
        return SpecSection(
            "Battery", listOf(
                SpecItem("Level", if (b.percent >= 0) "${b.percent} %" else UNKNOWN),
                SpecItem("Charging", if (b.charging) "yes" else "no"),
                SpecItem(
                    "Temperature",
                    if (b.temperatureC > 0) "%.1f C".format(b.temperatureC) else UNKNOWN
                ),
                SpecItem("Health", healthLabel)
            )
        )
    }

    // ---------------------------------------------------------------- screen

    private fun screenSection(): SpecSection {
        val dm = context.resources.displayMetrics
        val items = mutableListOf(
            SpecItem("Resolution", "${dm.widthPixels} x ${dm.heightPixels} px"),
            SpecItem("Density", "${dm.densityDpi} dpi (x${dm.density})"),
            SpecItem("Font scale", "%.2f".format(context.resources.configuration.fontScale))
        )
        try {
            val wm = context.getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
            @Suppress("DEPRECATION")
            val hz = wm.defaultDisplay.refreshRate
            items += SpecItem("Refresh rate", "%.0f Hz".format(hz))
        } catch (e: Exception) {
            // Display metrics are best effort; the section is useful without it.
        }
        return SpecSection("Screen", items)
    }

    // ----------------------------------------------------------------- utils

    private fun formatBytes(bytes: Long): String = when {
        bytes >= 1L shl 30 -> "%.2f GB".format(bytes.toDouble() / (1L shl 30))
        bytes >= 1L shl 20 -> "%.0f MB".format(bytes.toDouble() / (1L shl 20))
        else -> "$bytes B"
    }

    /** Flat text dump, used by the share action on the specs screen. */
    fun asPlainText(): String = buildString {
        val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
        appendLine("RTVIO Mapper - device specifications")
        appendLine("captured $stamp")
        collectAll().forEach { section ->
            appendLine()
            appendLine("[" + section.title + "]")
            section.items.forEach { appendLine("  " + it.label + ": " + it.value) }
        }
    }
}

/** Percent 0-100 (-1 unknown), charging flag, temperature in Celsius. */
data class BatteryState(val percent: Int, val charging: Boolean, val temperatureC: Double)
