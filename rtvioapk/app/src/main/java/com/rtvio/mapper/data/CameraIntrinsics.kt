package com.rtvio.mapper.data

import android.hardware.camera2.CameraCharacteristics
import kotlin.math.abs

/**
 * Camera intrinsic matrix K and distortion coefficients, recovered from Camera2 API.
 *
 * The intrinsic matrix projects 3D points to 2D image coordinates:
 *   [u]   [fx  0 cx] [X]
 *   [v] = [ 0 fy cy] [Y]
 *   [1]   [ 0  0  1] [Z]
 *
 * where fx, fy are focal lengths in pixels and (cx, cy) is the principal point.
 *
 * Camera2 API provides:
 *   - LENS_INTRINSIC_CALIBRATION = [fx_pix, fy_pix, cx_pix, cy_pix, skew]
 *     (available on LIMITED and above hardware levels)
 *   - LENS_INFO_AVAILABLE_FOCAL_LENGTHS in mm (limited to discrete values)
 *   - SENSOR_INFO_PHYSICAL_SIZE in mm
 *   - SENSOR_INFO_PIXEL_ARRAY_SIZE in pixels
 *
 * Not all devices expose LENS_INTRINSIC_CALIBRATION. When unavailable, compute from
 * focal length and sensor size:
 *   fx_pix = (focal_length_mm / sensor_width_mm) * image_width_px
 *   fy_pix = (focal_length_mm / sensor_height_mm) * image_height_px
 *   cx_pix ≈ image_width_px / 2
 *   cy_pix ≈ image_height_px / 2
 *
 * Distortion is typically negligible on modern phones (they're close to rectilinear),
 * so we default to zero coefficients. If the device reports distortion via a vendor
 * extension, it should be read and included here.
 */
data class CameraIntrinsics(
    // Focal length in pixels
    val fx_pix: Double,
    val fy_pix: Double,
    // Principal point in pixels
    val cx_pix: Double,
    val cy_pix: Double,
    // Radial distortion: k1, k2 (barrel or pincushion)
    // Tangential distortion: p1, p2 (asymmetric distortion)
    // Thin prism distortion: s1, s2 (rarely used)
    // Most phone cameras are close to distortion-free; defaults are zero.
    val k1: Double = 0.0,
    val k2: Double = 0.0,
    val p1: Double = 0.0,
    val p2: Double = 0.0,
    val k3: Double = 0.0,
    // Source: how these values were obtained
    val source: String
) {
    /**
     * The 3×3 intrinsic matrix, as a flat list (row-major) for JSON export.
     * [fx, 0, cx, 0, fy, cy, 0, 0, 1]
     */
    fun matrixFlat(): List<Double> = listOf(
        fx_pix, 0.0, cx_pix,
        0.0, fy_pix, cy_pix,
        0.0, 0.0, 1.0
    )

    /**
     * Distortion coefficients in OpenCV / camera_calibration order:
     * [k1, k2, p1, p2, k3]
     */
    fun distortionFlat(): List<Double> = listOf(k1, k2, p1, p2, k3)
}

/**
 * Intrinsics for the frames the app actually sends: the analysis buffer
 * ([bufferW] x [bufferH], sensor orientation) rotated upright by
 * [rotationDegrees], exactly as FrameEncoder rotates it.
 *
 * [cameraIntrinsicsFromCharacteristics] below describes a hypothetical image
 * of the requested size, which was wrong twice over on a real phone: the
 * calibration is in active-array pixels (e.g. 4000x3000), not the buffer's,
 * and the sent frames are portrait while the sensor is landscape. It also
 * passed through a (0, 0) principal point, which some devices report when
 * they do not populate it. This maps the calibration through the same
 * centred crop + scale CameraX applies to reach the buffer, then through the
 * rotation, and falls back to the image centre for a zero principal point.
 */
fun cameraIntrinsicsForOutput(
    characteristics: CameraCharacteristics,
    bufferW: Int,
    bufferH: Int,
    rotationDegrees: Int
): CameraIntrinsics {
    val active = characteristics.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
    val aw = (active?.width() ?: bufferW).toDouble()
    val ah = (active?.height() ?: bufferH).toDouble()

    val calib = characteristics.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION)
    var fx: Double
    var fy: Double
    var cx: Double
    var cy: Double
    val source: String
    if (calib != null && calib.size >= 4 && calib[0] > 0f && calib[1] > 0f) {
        fx = calib[0].toDouble(); fy = calib[1].toDouble()
        cx = calib[2].toDouble(); cy = calib[3].toDouble()
        if (cx <= 0.0 || cy <= 0.0) { cx = aw / 2; cy = ah / 2 }
        source = "Camera2 LENS_INTRINSIC_CALIBRATION, mapped to ${bufferW}x$bufferH rot $rotationDegrees"
    } else {
        val f = characteristics.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.firstOrNull() ?: 0f
        val sensor = characteristics.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
        if (f > 0f && sensor != null && sensor.width > 0f) {
            fx = f / sensor.width * aw
            fy = f / sensor.height * ah
            source = "focal length %.2f mm / sensor %.2fx%.2f mm, mapped to %dx%d rot %d".format(
                f, sensor.width, sensor.height, bufferW, bufferH, rotationDegrees)
        } else {
            fx = aw * 0.8; fy = fx
            source = "no calibration or focal length - nominal guess"
        }
        cx = aw / 2; cy = ah / 2
    }

    // CameraX fills the buffer from a centred crop of the active array that
    // has the buffer's aspect ratio, scaled by k active pixels per buffer pixel.
    val k = minOf(aw / bufferW, ah / bufferH)
    val offX = (aw - bufferW * k) / 2
    val offY = (ah - bufferH * k) / 2
    fx /= k; fy /= k
    cx = (cx - offX) / k; cy = (cy - offY) / k

    // Same pixel mapping as FrameEncoder: 90 -> (u', v') = (H-1-v, u),
    // 270 -> (u', v') = (v, W-1-u), 180 -> (W-1-u, H-1-v).
    val rot = ((rotationDegrees % 360) + 360) % 360
    return when (rot) {
        90 -> CameraIntrinsics(fy, fx, bufferH - 1 - cy, cx, source = source)
        270 -> CameraIntrinsics(fy, fx, cy, bufferW - 1 - cx, source = source)
        180 -> CameraIntrinsics(fx, fy, bufferW - 1 - cx, bufferH - 1 - cy, source = source)
        else -> CameraIntrinsics(fx, fy, cx, cy, source = source)
    }
}

/**
 * Recover intrinsics from Camera2 characteristics. Prefers the authoritative
 * LENS_INTRINSIC_CALIBRATION when available, falls back to computed from
 * focal length and sensor geometry.
 */
fun cameraIntrinsicsFromCharacteristics(
    characteristics: CameraCharacteristics,
    imageWidthPx: Int,
    imageHeightPx: Int
): CameraIntrinsics {
    // LENS_INTRINSIC_CALIBRATION is the ground truth if present.
    // Returns [fx_pix, fy_pix, cx_pix, cy_pix, skew] or null if unavailable.
    val calibration = characteristics.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION)
    if (calibration != null && calibration.size >= 4) {
        return CameraIntrinsics(
            fx_pix = calibration[0].toDouble(),
            fy_pix = calibration[1].toDouble(),
            cx_pix = calibration[2].toDouble(),
            cy_pix = calibration[3].toDouble(),
            // Skew is rarely nonzero on phones; if this ever matters, it's calibration[4].
            source = "Camera2 LENS_INTRINSIC_CALIBRATION"
        )
    }

    // Fallback: derive from focal length and sensor geometry.
    // This is less accurate (phone lenses have subtle nonlinearities and
    // asymmetries) but sufficient for initialization.
    val focalLengths = characteristics.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
        ?: FloatArray(0)
    val sensorSize = characteristics.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
    val focalLengthMm = focalLengths.firstOrNull() ?: 0f
    val sensorWidthMm = sensorSize?.width ?: 0f
    val sensorHeightMm = sensorSize?.height ?: 0f

    // Compute pixel focal lengths. The formula assumes thin lens:
    //   f_px = f_mm * (sensor_width_px / sensor_width_mm)
    // But we don't know sensor_width_px directly; infer from aspect ratio.
    val fx_pix: Double
    val fy_pix: Double
    if (focalLengthMm > 0f && sensorWidthMm > 0f && sensorHeightMm > 0f) {
        // Assume the image aspect ratio matches the sensor aspect ratio.
        val sensorAspect = sensorWidthMm / sensorHeightMm
        val imageAspect = imageWidthPx.toDouble() / imageHeightPx
        // If they're close, use the aspect ratio to infer the sensor's pixel width.
        val sensorAspectError = abs(sensorAspect - imageAspect) / sensorAspect
        if (sensorAspectError < 0.1) {  // Tolerate small differences (crops, rotations)
            // Aspect matches: assume the image covers the full sensor.
            fx_pix = ((focalLengthMm / sensorWidthMm) * imageWidthPx).toDouble()
            fy_pix = ((focalLengthMm / sensorHeightMm) * imageHeightPx).toDouble()
        } else {
            // Aspect doesn't match (e.g., cropped or rotated). Use the horizontal dimension only.
            fx_pix = ((focalLengthMm / sensorWidthMm) * imageWidthPx).toDouble()
            fy_pix = fx_pix  // Assume square pixels; correct for real deviations if needed.
        }
    } else {
        // No focal length metadata. Use a plausible default.
        // Standard smartphone field of view is ~52° horizontal, which corresponds
        // to ~0.9× the diagonal at standard 16:9 aspect ratio. This is a last resort.
        fx_pix = imageWidthPx * 0.5
        fy_pix = imageHeightPx * 0.5
    }

    return CameraIntrinsics(
        fx_pix = fx_pix,
        fy_pix = fy_pix,
        cx_pix = imageWidthPx / 2.0,
        cy_pix = imageHeightPx / 2.0,
        source = "computed from focal_length=%.2f mm, sensor=%.2fx%.2f mm, image=%dx%d px".format(
            focalLengthMm, sensorWidthMm, sensorHeightMm, imageWidthPx, imageHeightPx
        )
    )
}
