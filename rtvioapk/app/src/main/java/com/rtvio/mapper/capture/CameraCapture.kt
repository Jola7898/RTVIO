package com.rtvio.mapper.capture

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.os.SystemClock
import android.util.Log
import android.util.Range
import android.util.Size
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.core.AspectRatio
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs

/**
 * The capture half of the pipeline: live preview plus a JPEG frame callback.
 *
 * Built on CameraX's camera-camera2 backend rather than raw Camera2. That is
 * the same Camera2 HAL underneath, but CameraX handles the session lifecycle,
 * per-device quirks and surface management.
 *
 * ENCODING IN PARALLEL, DELIVERING IN ORDER
 *
 * Software JPEG is the phone's frame-rate ceiling (a real 4 MP capture ran at
 * ~15 fps). Each frame is split in two: the analyzer thread copies the camera
 * planes into one of [ENCODER_SLOTS] encoders' own buffers (fast, and it has
 * to happen before the ImageProxy is closed), then JPEG compression runs on a
 * pool of [ENCODE_THREADS] threads. Futures are queued in submission order
 * and one emitter thread waits on them in that order, so frames reach the
 * listener exactly in capture order however the compressions interleave.
 * If every encoder slot is busy the camera frame is skipped - and counted in
 * [skippedCount], which is the number to watch if the rate falls short.
 *
 * TIMESTAMPS
 *
 * Each frame is stamped with its sensor exposure time (ImageInfo.timestamp)
 * converted to wall-clock ms, not the moment encoding finished: the old
 * after-encode stamp carried tens of milliseconds of encode-time jitter.
 */
class CameraCapture(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val previewView: PreviewView
) {

    private companion object {
        const val TAG = "CameraCapture"
        const val ENCODER_SLOTS = 4
        const val ENCODE_THREADS = 2
        const val PREVIEW_INTERVAL_NS = 500_000_000L   // 2 viewfinder images/s to the desktop
        const val PREVIEW_QUALITY = 45
    }

    /**
     * @param jpeg valid only for the duration of the callback - the encoder
     *   reuses its buffer for the next frame.
     * @param timestampMs wall-clock capture time, the frame packet's timestamp.
     */
    fun interface FrameListener {
        fun onFrame(jpeg: FrameEncoder.Jpeg, timestampMs: Long)
    }

    private class Encoded(
        val encoder: FrameEncoder,
        val jpeg: FrameEncoder.Jpeg?,
        val timestampMs: Long,
        val isPreview: Boolean
    )

    private var cameraProvider: ProcessCameraProvider? = null
    private var analysisExecutor: ExecutorService? = null
    private var encodeExecutor: ExecutorService? = null
    private var emitterThread: Thread? = null
    private val encoders = ArrayBlockingQueue<FrameEncoder>(ENCODER_SLOTS)
    private val inflight = LinkedBlockingQueue<Future<Encoded>>()
    @Volatile private var emitting = false

    @Volatile private var jpegQuality = 70
    @Volatile private var frameIntervalNs = 0L
    private var lastAcceptedNs = 0L
    private var lastPreviewNs = 0L

    private val framesThisWindow = AtomicLong()
    private var windowStartNs = System.nanoTime()
    @Volatile var measuredFps: Double = 0.0
        private set

    /** Resolution the camera actually gave us, which may differ from the request. */
    @Volatile var activeResolution: Size? = null
        private set
    /** Size of the (rotated, upright) JPEGs actually produced, once known. */
    @Volatile var outputSize: Size? = null
        private set
    @Volatile var rotationDegrees: Int = 0
        private set

    @Volatile var isRunning: Boolean = false
        private set

    /** Frames accepted for recording/streaming since [resetCounters]. */
    val capturedCount = AtomicLong()
    /** Frames delivered to the listener. */
    val encodedCount = AtomicLong()
    /** Camera frames skipped because every encoder was busy. */
    val skippedCount = AtomicLong()
    private val encodeNsTotal = AtomicLong()
    private val encodeNsCount = AtomicLong()
    private val inflightCount = AtomicInteger()

    /** Mean JPEG compression time since [resetCounters], ms. */
    val meanEncodeMs: Double
        get() = encodeNsCount.get().let { if (it == 0L) 0.0 else encodeNsTotal.get() / it / 1e6 }
    /** Frames between the camera and the listener right now. */
    val framesInFlight: Int get() = inflightCount.get()

    /**
     * Gates the expensive half of the pipeline. The preview is bound whenever
     * the screen is visible so the operator can frame the shot, but frames
     * only go to the listener while this is true - flipping it is what makes
     * START instant.
     */
    @Volatile var streaming: Boolean = false

    /** Low-rate, low-quality viewfinder images while not streaming (null = off). */
    @Volatile var previewListener: FrameListener? = null

    fun resetCounters() {
        capturedCount.set(0); encodedCount.set(0); skippedCount.set(0)
        encodeNsTotal.set(0); encodeNsCount.set(0)
    }

    fun start(
        requested: Size,
        targetFps: Int,
        quality: Int,
        aspect43: Boolean,
        listener: FrameListener,
        onError: (String) -> Unit,
        onBound: (() -> Unit)? = null
    ) {
        jpegQuality = quality.coerceIn(1, 100)
        frameIntervalNs = if (targetFps > 0) 1_000_000_000L / targetFps else 0L
        lastAcceptedNs = 0L
        encoders.clear()
        repeat(ENCODER_SLOTS) { encoders.offer(FrameEncoder()) }
        encodeExecutor = Executors.newFixedThreadPool(ENCODE_THREADS) { r ->
            Thread(r, "rtvio-jpeg").apply { priority = Thread.MAX_PRIORITY - 1 }
        }
        startEmitter(listener)

        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            try {
                val provider = future.get()
                cameraProvider = provider
                bind(provider, requested, targetFps, aspect43)
                isRunning = true
                onBound?.invoke()
            } catch (e: Exception) {
                Log.e(TAG, "camera bind failed", e)
                onError(e.message ?: "Camera could not be opened")
            }
        }, ContextCompat.getMainExecutor(context))
    }

    private fun startEmitter(listener: FrameListener) {
        emitting = true
        emitterThread = Thread({
            while (emitting) {
                val f = try {
                    inflight.poll(200, TimeUnit.MILLISECONDS) ?: continue
                } catch (e: InterruptedException) {
                    break
                }
                val e = try {
                    f.get()
                } catch (ex: Exception) {
                    Log.w(TAG, "encode failed: ${ex.message}")
                    inflightCount.decrementAndGet()
                    continue
                }
                try {
                    val jpeg = e.jpeg
                    if (jpeg != null) {
                        if (e.isPreview) {
                            previewListener?.onFrame(jpeg, e.timestampMs)
                        } else {
                            encodedCount.incrementAndGet()
                            listener.onFrame(jpeg, e.timestampMs)
                        }
                    }
                } catch (ex: Exception) {
                    Log.w(TAG, "frame listener failed: ${ex.message}")
                } finally {
                    encoders.offer(e.encoder)
                    inflightCount.decrementAndGet()
                }
            }
        }, "rtvio-frame-emitter").apply { priority = Thread.MAX_PRIORITY - 1; start() }
    }

    // No @OptIn here: ExperimentalCamera2Interop is not declared as a Kotlin
    // opt-in requirement marker in camera-camera2 1.3.1, so annotating for it
    // is silently ignored and only produces a warning of its own.
    private fun bind(provider: ProcessCameraProvider, requested: Size, targetFps: Int, aspect43: Boolean) {
        provider.unbindAll()

        val selector = CameraSelector.DEFAULT_BACK_CAMERA

        // 4:3 is the sensor's native shape on almost every phone, i.e. the full
        // field of view; 16:9 crops it. FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER
        // keeps at least the requested pixel count where the device can.
        val resolutionSelector = ResolutionSelector.Builder()
            .setAspectRatioStrategy(
                if (aspect43) AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY
                else AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY
            )
            .setResolutionStrategy(
                ResolutionStrategy(requested, ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER)
            )
            .build()

        val preview = Preview.Builder()
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(
                        if (aspect43) AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY
                        else AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY
                    ).build()
            )
            .build().also { it.setSurfaceProvider(previewView.surfaceProvider) }

        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(resolutionSelector)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            // Never queue inside CameraX: our own encoder slots are the buffer.
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
        analysis.setAnalyzer(executor) { proxy -> handleFrame(proxy) }

        provider.bindToLifecycle(lifecycleOwner, selector, preview, analysis)
        activeResolution = analysis.resolutionInfo?.resolution
        Log.i(TAG, "bound analysis at ${activeResolution} (requested $requested, 4:3=$aspect43)")
    }

    private fun handleFrame(proxy: ImageProxy) {
        try {
            val now = System.nanoTime()
            // Wall-clock time of the exposure. CameraX's timestamp is on the
            // elapsedRealtime clock on virtually every device (REALTIME source);
            // if it is not within a second of it, fall back to "now".
            val wallNow = System.currentTimeMillis()
            val sinceCapture = SystemClock.elapsedRealtimeNanos() - proxy.imageInfo.timestamp
            val captureMs = if (sinceCapture in 0..1_000_000_000L) wallNow - sinceCapture / 1_000_000L else wallNow

            // Gate to the configured rate. The 10% tolerance matters: without
            // it, ordinary jitter around an exactly-on-target delivery rate
            // would reject every other frame and halve the effective FPS.
            if (frameIntervalNs > 0 && lastAcceptedNs != 0L) {
                if (now - lastAcceptedNs < frameIntervalNs - frameIntervalNs / 10) return
            }
            lastAcceptedNs = now
            tickFps(now)

            val stream = streaming
            val preview = !stream && previewListener != null && now - lastPreviewNs >= PREVIEW_INTERVAL_NS
            if (!stream && !preview) return
            if (stream) capturedCount.incrementAndGet()

            val enc = encoders.poll()
            if (enc == null) {
                if (stream) skippedCount.incrementAndGet()
                return
            }
            if (preview) lastPreviewNs = now
            rotationDegrees = proxy.imageInfo.rotationDegrees
            try {
                enc.prepare(proxy, rotationDegrees)
            } catch (e: Exception) {
                encoders.offer(enc)
                throw e
            }
            val quality = if (stream) jpegQuality else PREVIEW_QUALITY
            val pool = encodeExecutor
            if (pool == null) {
                encoders.offer(enc)
                return
            }
            inflightCount.incrementAndGet()
            inflight.put(pool.submit<Encoded> {
                val t0 = System.nanoTime()
                val jpeg = enc.compress(quality)
                if (!preview) {
                    encodeNsTotal.addAndGet(System.nanoTime() - t0)
                    encodeNsCount.incrementAndGet()
                }
                outputSize = Size(jpeg.width, jpeg.height)
                Encoded(enc, jpeg, captureMs, preview)
            })
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
        // Let already-submitted frames finish and be emitted, then stop.
        encodeExecutor?.shutdown()
        encodeExecutor = null
        emitterThread?.let { t ->
            Thread {
                val deadline = System.currentTimeMillis() + 2_000
                while (inflightCount.get() > 0 && System.currentTimeMillis() < deadline) Thread.sleep(20)
                emitting = false
                t.interrupt()
            }.start()
        }
        emitterThread = null
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
