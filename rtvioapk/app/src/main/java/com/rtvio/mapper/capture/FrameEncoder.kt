package com.rtvio.mapper.capture

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import android.util.Size
import java.io.ByteArrayOutputStream

/**
 * Turns a camera's YUV_420_888 frame into an upright JPEG.
 *
 * Two things make this fast enough to run at 30 fps on mid-range hardware:
 *
 * 1. **Rotation happens during the NV21 assembly, not after it.** The frame has
 *    to be copied out of the three hardware planes into one contiguous NV21
 *    buffer regardless, so the copy writes straight to rotated destination
 *    offsets. That saves a whole extra pass and a second multi-megabyte buffer
 *    versus the usual "convert, then rotate" approach.
 *
 * 2. **Every buffer is reused.** At 1080p a frame is ~3 MB of NV21 plus the
 *    JPEG; allocating those 30 times a second would keep the GC permanently
 *    busy and show up as frame-time jitter.
 *
 * An instance is therefore **not thread-safe** and must be confined to the
 * single camera analysis thread.
 */
class FrameEncoder {

    private var nv21 = ByteArray(0)
    private var rowBuffer = ByteArray(0)
    private var uRow = ByteArray(0)
    private var vRow = ByteArray(0)
    private val jpegOut = ExposedByteArrayOutputStream(512 * 1024)

    /** The JPEG produced by the most recent [encode]; valid until the next call. */
    class Jpeg(val bytes: ByteArray, val length: Int, val width: Int, val height: Int)

    private var preparedW = 0
    private var preparedH = 0

    /**
     * @param image a YUV_420_888 frame; not closed here, the caller owns it.
     * @param rotationDegrees 0, 90, 180 or 270 - the value CameraX reports as
     *   ImageInfo.rotationDegrees, i.e. how far the buffer must be rotated
     *   clockwise to appear upright.
     * @param quality JPEG quality, 1-100.
     */
    fun encode(image: ImageProxy, rotationDegrees: Int, quality: Int): Jpeg {
        prepare(image, rotationDegrees)
        return compress(quality)
    }

    /**
     * First half of [encode]: copies (and rotates) the camera planes into this
     * encoder's own NV21 buffer. Must run while [image] is still open; after
     * it returns the image can be closed and [compress] can run on any thread.
     * Splitting the two is what lets CameraCapture encode frames in parallel.
     */
    fun prepare(image: ImageProxy, rotationDegrees: Int) {
        require(image.format == ImageFormat.YUV_420_888) {
            "expected YUV_420_888, got format ${image.format}"
        }
        val srcW = image.width
        val srcH = image.height
        val needed = srcW * srcH * 3 / 2
        if (nv21.size < needed) nv21 = ByteArray(needed)

        val rot = ((rotationDegrees % 360) + 360) % 360
        require(rot % 90 == 0) { "rotation must be a multiple of 90, got $rotationDegrees" }

        writeLuma(image, srcW, srcH, rot)
        writeChroma(image, srcW, srcH, rot)

        // A 90/270 turn swaps the axes.
        preparedW = if (rot == 90 || rot == 270) srcH else srcW
        preparedH = if (rot == 90 || rot == 270) srcW else srcH
    }

    /** Second half of [encode]: JPEG-compresses the buffer [prepare] filled. */
    fun compress(quality: Int): Jpeg {
        jpegOut.reset()
        YuvImage(nv21, ImageFormat.NV21, preparedW, preparedH, null)
            .compressToJpeg(Rect(0, 0, preparedW, preparedH), quality, jpegOut)
        return Jpeg(jpegOut.buffer(), jpegOut.size(), preparedW, preparedH)
    }

    /** Output dimensions [encode] would produce, without doing the work. */
    fun outputSize(srcW: Int, srcH: Int, rotationDegrees: Int): Size {
        val rot = ((rotationDegrees % 360) + 360) % 360
        return if (rot == 90 || rot == 270) Size(srcH, srcW) else Size(srcW, srcH)
    }

    // ------------------------------------------------------------------ luma

    private fun writeLuma(image: ImageProxy, srcW: Int, srcH: Int, rot: Int) {
        val plane = image.planes[0]
        val buf = plane.buffer
        val rowStride = plane.rowStride
        val pixelStride = plane.pixelStride

        // The common case: tightly packed rows, no rotation. One bulk copy.
        if (rot == 0 && pixelStride == 1 && rowStride == srcW) {
            buf.position(0)
            buf.get(nv21, 0, srcW * srcH)
            return
        }

        if (rowBuffer.size < rowStride) rowBuffer = ByteArray(rowStride)
        val row = rowBuffer

        for (r in 0 until srcH) {
            buf.position(r * rowStride)
            // The final row is short by (pixelStride - 1) bytes in the buffer,
            // so never ask for more than what remains.
            buf.get(row, 0, minOf(rowStride, buf.remaining()))
            when (rot) {
                0 -> {
                    val base = r * srcW
                    for (x in 0 until srcW) nv21[base + x] = row[x * pixelStride]
                }
                90 -> {
                    val col = srcH - 1 - r
                    for (x in 0 until srcW) nv21[x * srcH + col] = row[x * pixelStride]
                }
                180 -> {
                    val base = (srcH - 1 - r) * srcW + srcW - 1
                    for (x in 0 until srcW) nv21[base - x] = row[x * pixelStride]
                }
                else -> { // 270
                    for (x in 0 until srcW) nv21[(srcW - 1 - x) * srcH + r] = row[x * pixelStride]
                }
            }
        }
    }

    // ---------------------------------------------------------------- chroma

    /**
     * Writes the interleaved V,U plane.
     *
     * NV21 orders chroma as V then U - the opposite of the plane order in
     * YUV_420_888, where planes[1] is U and planes[2] is V. Getting this
     * backwards produces an image with red and blue swapped, which is the
     * classic symptom to look for if this ever regresses.
     */
    private fun writeChroma(image: ImageProxy, srcW: Int, srcH: Int, rot: Int) {
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]
        val uBuf = uPlane.buffer
        val vBuf = vPlane.buffer
        val uRowStride = uPlane.rowStride
        val vRowStride = vPlane.rowStride
        val uPixelStride = uPlane.pixelStride
        val vPixelStride = vPlane.pixelStride

        val cW = srcW / 2
        val cH = srcH / 2
        val base = srcW * srcH

        if (uRow.size < uRowStride) uRow = ByteArray(uRowStride)
        if (vRow.size < vRowStride) vRow = ByteArray(vRowStride)

        for (cy in 0 until cH) {
            uBuf.position(cy * uRowStride)
            uBuf.get(uRow, 0, minOf(uRowStride, uBuf.remaining()))
            vBuf.position(cy * vRowStride)
            vBuf.get(vRow, 0, minOf(vRowStride, vBuf.remaining()))

            when (rot) {
                0 -> {
                    var d = base + cy * srcW
                    for (cx in 0 until cW) {
                        nv21[d++] = vRow[cx * vPixelStride]
                        nv21[d++] = uRow[cx * uPixelStride]
                    }
                }
                90 -> {
                    // Destination chroma rows are cW tall and srcH bytes wide.
                    val col = (cH - 1 - cy) * 2
                    for (cx in 0 until cW) {
                        val d = base + cx * srcH + col
                        nv21[d] = vRow[cx * vPixelStride]
                        nv21[d + 1] = uRow[cx * uPixelStride]
                    }
                }
                180 -> {
                    val rowBase = base + (cH - 1 - cy) * srcW
                    for (cx in 0 until cW) {
                        val d = rowBase + (cW - 1 - cx) * 2
                        nv21[d] = vRow[cx * vPixelStride]
                        nv21[d + 1] = uRow[cx * uPixelStride]
                    }
                }
                else -> { // 270
                    val col = cy * 2
                    for (cx in 0 until cW) {
                        val d = base + (cW - 1 - cx) * srcH + col
                        nv21[d] = vRow[cx * vPixelStride]
                        nv21[d + 1] = uRow[cx * uPixelStride]
                    }
                }
            }
        }
    }

    /**
     * ByteArrayOutputStream that hands back its backing array instead of a copy.
     *
     * toByteArray() would allocate and copy the whole JPEG on every frame; at
     * 30 fps that is tens of megabytes a second of pure garbage. Callers must
     * respect the paired length and not retain the array past the next reset.
     */
    private class ExposedByteArrayOutputStream(size: Int) : ByteArrayOutputStream(size) {
        fun buffer(): ByteArray = buf
    }
}
