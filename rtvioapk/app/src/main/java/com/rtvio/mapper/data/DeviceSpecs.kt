package com.rtvio.mapper.data

/** One label/value row on the specs screen. */
data class SpecItem(val label: String, val value: String)

/** A titled group of rows. */
data class SpecSection(val title: String, val items: List<SpecItem>)

/**
 * The camera facts the desktop reconstruction actually needs as a starting
 * guess for intrinsics. Kept as a struct (not just display strings) because
 * these are the numbers worth exporting alongside a capture session.
 */
data class CameraSpecs(
    val cameraId: String,
    val lensFacing: String,
    val maxJpegSize: android.util.Size?,
    val focalLengthsMm: FloatArray,
    val sensorWidthMm: Float,
    val sensorHeightMm: Float,
    val horizontalFovDeg: Double,
    val verticalFovDeg: Double,
    val maxFpsAtDefaultSize: Double,
    val supportedFormats: List<String>,
    val hardwareLevel: String
) {
    override fun equals(other: Any?): Boolean = this === other
    override fun hashCode(): Int = cameraId.hashCode()
}
