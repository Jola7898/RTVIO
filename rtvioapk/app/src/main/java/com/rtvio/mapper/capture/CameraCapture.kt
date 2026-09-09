package com.rtvio.mapper.capture

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.util.Log
import android.util.Range
import android.util.Size
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs

/**
 * The capture half of the pipeline: live preview plus a JPEG frame callback.
 *
 * Built on CameraX's camera-camera2 backend rather than raw Camera2. That is
 * the same Camera2 HAL underneath, but CameraX handles the session lifecycle,
 * per-device quirks and surface management that make hand-rolled Camera2 the
 * single largest source of crashes in apps like this one.
 *
 * Frames arrive on a dedicated single-thread executor, and the [FrameEncoder]
 * lives on that thread so its reusable buffers stay confined to it.
 */
class CameraCapture(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val previewView: PreviewView
) {

    private companion object {
        const val TAG = "CameraCapture"
    }

    /**
     * @param jpeg valid only for the duration of the callback - the encoder
     *   reuses its buffer for the next frame.
     * @param timestampMs wall-clock capture time, the frame packet's timestamp.
     */
    fun interface FrameListener {
        fun onFrame(jpeg: FrameEncoder.Jpeg, timestampMs: Long)
    }

    private var cameraProvider: ProcessCameraProvider? = null
    private var analysisExecutor: ExecutorService? = null
    private val encoder = FrameEncoder()

    @Volatile private var jpegQuality = 70
    @Volatile private var frameIntervalNs = 0L
    private var lastAcceptedNs = 0L

    private val framesThisWindow = AtomicLong()
    private var windowStartNs = System.nanoTime()
    @Volatile var measuredFps: Double = 0.0
        private set

    /** Resolution the camera actually gave us, which may differ from the request. */
    @Volatile var activeResolution: Size? = null
        private set

    @Volatile var isRunning: Boolean = false
        private set

    /**
     * Gates the expensive half of the pipeline.
     *
     * The preview is bound whenever the screen is visible so the operator can
     * frame the shot before committing, but JPEG encoding only happens while
     * this is true. Flipping it is also what makes START instant: the camera
     * session is already warm, so there is no open-and-configure delay between
     * the tap and the first frame on the wire.
     */
    @Volatile var streaming: Boolean = false

    fun start(
        requested: Size,
        targetFps: Int,
        quality: Int,
        listener: FrameListener,
        onError: (String) -> Unit
    ) {
        jpegQuality = quality.coerceIn(1, 100)
        frameIntervalNs = if (targetFps > 0) 1_000_000_000L / targetFps else 0L
        lastAcceptedNs = 0L

        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            try {
                val provider = future.get()
                cameraProvider = provider
                bind(provider, requested, targetFps, listener)
                isRunning = true
            } catch (e: Exception) {
                Log.e(TAG, "camera bind failed", e)
                onError(e.message ?: "Camera could not be opened")
            }
        }, ContextCompat.getMainExecutor(context))
    }

    // No @OptIn here: ExperimentalCamera2Interop is not declared as a Kotlin
    // opt-in requirement marker in camera-camera2 1.3.1, so annotating for it
    // is silently ignored and only produces a warning of its own.
    private fun bind(
        provider: ProcessCameraProvider,
        requested: Size,
        targetFps: Int,
        listener: FrameListener
    ) {
        provider.unbindAll()

        val selector = CameraSelector.DEFAULT_BACK_CAMERA

        // FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER: prefer at least the requested
        // pixel count so we never silently downgrade a 1080p session to 640x480
        // on a device that lacks the exact size.
        val resolutionSelector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(
                    requested,
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER
                )
            )
            .build()

        val preview = Preview.Builder().build().also {
            it.setSurfaceProvider(previewView.surfaceProvider)
        }

        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(resolutionSelector)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            // Never queue: an old frame is worth less than a fresh one, and
            // queueing would add latency the operator cannot see or control.
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)

        // Ask the sensor itself for the target rate where the device supports it.
        // Software gating below still enforces the cap on devices that do not.
        supportedFpsRange(targetFps)?.let { range ->
            Camera2Interop.Extender(analysisBuilder)
                .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, range)
            Log.i(TAG, "AE target FPS range set to $range")
        }

        val analysis = analysisBuilder.build()

        val executor = Executors.newSingleThreadExecutor { r ->
            Thread(r, "rtvio-camera-analysis").apply { priority = Thread.MAX_PRIORITY }
        }
        analysisExecutor = executor

        analysis.setAnalyzer(executor) { proxy -> handleFrame(proxy, listener) }

        provider.bindToLifecycle(lifecycleOwner, selector, preview, analysis)
        activeResolution = analysis.resolutionInfo?.resolution
        Log.i(TAG, "bound analysis at ${activeResolution} (requested $requested)")
    }

    private fun handleFrame(proxy: ImageProxy, listener: FrameListener) {
        try {
            val now = System.nanoTime()
            // Gate to the configured rate. The 10% tolerance matters: without
            // it, ordinary jitter around an exactly-on-target delivery rate
            // would reject every other frame and halve the effective FPS.
            if (frameIntervalNs > 0 && lastAcceptedNs != 0L) {
                if (now - lastAcceptedNs < frameIntervalNs - frameIntervalNs / 10) return
            }
            lastAcceptedNs = now
            // Counted before the streaming gate so the overlay shows the real
            // capture cadence while the operator is still framing the shot.
            tickFps(now)
            if (!streaming) return

            val jpeg = encoder.encode(proxy, proxy.imageInfo.rotationDegrees, jpegQuality)
            listener.onFrame(jpeg, System.currentTimeMillis())
        } catch (e: Exception) {
            // A single bad frame must not tear down the session.
            Log.w(TAG, "frame dropped: ${e.message}")
        } finally {
            proxy.close()
        }
    }

    private fun tickFps(nowNs: Long) {
        val n = framesThisWindow.incrementAndGet()
        val elapsed = nowNs - windowStartNs
        if (elapsed >= 1_000_000_000L) {
            measuredFps = n * 1e9 / elapsed
            framesThisWindow.set(0)
            windowStartNs = nowNs
        }
    }

    /** Applies a new JPEG quality without rebuilding the camera session. */
    fun updateQuality(quality: Int) {
        jpegQuality = quality.coerceIn(1, 100)
    }

    fun stop() {
        isRunning = false
        streaming = false
        measuredFps = 0.0
        try {
            cameraProvider?.unbindAll()
        } catch (e: Exception) {
            Log.w(TAG, "unbind failed", e)
        }
        cameraProvider = null
        analysisExecutor?.shutdown()
        analysisExecutor = null
    }

    /**
     * Picks an AE target range the hardware actually advertises.
     *
     * Setting an unsupported range is not a no-op on every device - some
     * reject the capture request outright and the session never produces a
     * frame. So we only ever set one the characteristics list, preferring a
     * fixed [target, target] lock over a variable range so exposure hunting
     * cannot silently halve the frame rate in low light.
     */
    private fun supportedFpsRange(targetFps: Int): Range<Int>? = try {
        val cm = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it)
                .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        } ?: cm.cameraIdList.firstOrNull()

        val ranges = id?.let {
            cm.getCameraCharacteristics(it)
                .get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
        }?.toList().orEmpty()

        ranges.firstOrNull { it.lower == targetFps && it.upper == targetFps }
            ?: ranges.filter { it.upper == targetFps }.minByOrNull { targetFps - it.lower }
            ?: ranges.filter { it.contains(targetFps) }.minByOrNull { it.upper - it.lower }
            ?: ranges.minByOrNull { abs(it.upper - targetFps) }
    } catch (e: Exception) {
        Log.w(TAG, "could not read AE FPS ranges", e)
        null
    }
}
