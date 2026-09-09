package com.rtvio.mapper.sensors

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import com.rtvio.mapper.net.ImuSample
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.sqrt

/**
 * Collects accelerometer and gyroscope data and hands it out in batches.
 *
 * ## Why the two sensors need reconciling
 *
 * The wire format carries one timestamp per sample with accelerometer *and*
 * gyroscope values on it. Android does not deliver them that way: the two
 * sensors fire independently, each with its own timestamp, and even at the same
 * requested rate their events interleave with drifting phase.
 *
 * The naive fix - pair each accelerometer event with the most recent gyroscope
 * reading - leaves the gyroscope up to one full sample period stale. At 100 Hz
 * that is 10 ms, and during a 90 deg/s pan it is a 0.9 deg attitude error
 * injected into every sample, which a downstream VIO filter will happily
 * integrate into drift.
 *
 * So instead this class **linearly interpolates the gyroscope onto each
 * accelerometer timestamp**. That requires a gyroscope sample on each side of
 * the accelerometer one, so accelerometer events are held until the bracketing
 * gyroscope sample arrives - a delay of at most one sample period, paid once,
 * in exchange for properly time-aligned samples.
 *
 * ## Threading
 *
 * Sensor callbacks and the batch flush both run on one dedicated HandlerThread,
 * so the internal buffers need no locking. The batch callback fires on that
 * same thread; it must not block.
 */
class SensorDataCollector(context: Context) : SensorEventListener {

    private companion object {
        const val TAG = "SensorCollector"
        /** Accelerometer events held while waiting for a bracketing gyro sample. */
        const val MAX_PENDING = 256
    }

    private val sensorManager =
        context.getSystemService(Context.SENSOR_SERVICE) as SensorManager

    private val accelerometer: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val gyroscope: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)

    val hasAccelerometer get() = accelerometer != null
    val hasGyroscope get() = gyroscope != null

    private var thread: HandlerThread? = null
    private var handler: Handler? = null

    private var onBatch: ((List<ImuSample>) -> Unit)? = null
    private var batchIntervalMs = 50L

    /** Accelerometer readings awaiting a gyroscope sample to interpolate against. */
    private val pendingAccel = ArrayDeque<AccelEvent>()
    private var batch = ArrayList<ImuSample>(64)

    // The two most recent gyroscope samples, oldest in `prev`.
    private var prevGyro: GyroEvent? = null
    private var lastGyro: GyroEvent? = null

    private val totalSamples = AtomicLong()
    val samplesCollected: Long get() = totalSamples.get()

    /** Angular rate magnitude in rad/s, for the on-screen stability indicator. */
    @Volatile var gyroMagnitude: Float = 0f
        private set

    private class AccelEvent(val t: Long, val x: Float, val y: Float, val z: Float)
    private class GyroEvent(val t: Long, val x: Float, val y: Float, val z: Float)

    /**
     * @param rateHz requested sampling rate; the platform treats it as a hint
     *   and typically delivers at or above it.
     * @param onBatch invoked on the collector thread every [batchIntervalMs]
     *   with the samples accumulated since the previous call.
     */
    fun start(rateHz: Int, batchIntervalMs: Long, onBatch: (List<ImuSample>) -> Unit) {
        if (thread != null) return
        if (accelerometer == null) {
            Log.e(TAG, "no accelerometer on this device; IMU streaming disabled")
            return
        }
        this.onBatch = onBatch
        this.batchIntervalMs = batchIntervalMs.coerceIn(10L, 500L)
        totalSamples.set(0)

        val t = HandlerThread("rtvio-imu", android.os.Process.THREAD_PRIORITY_URGENT_AUDIO)
        t.start()
        thread = t
        val h = Handler(t.looper)
        handler = h

        val periodUs = 1_000_000 / rateHz.coerceIn(1, 1000)
        sensorManager.registerListener(this, accelerometer, periodUs, h)
        if (gyroscope != null) {
            sensorManager.registerListener(this, gyroscope, periodUs, h)
        } else {
            Log.w(TAG, "no gyroscope; samples will carry zero angular rate")
        }

        h.postDelayed(flushRunnable, this.batchIntervalMs)
    }

    fun stop() {
        try {
            sensorManager.unregisterListener(this)
        } catch (e: Exception) {
            Log.w(TAG, "unregister failed", e)
        }
        handler?.removeCallbacks(flushRunnable)
        thread?.quitSafely()
        thread = null
        handler = null
        onBatch = null
        pendingAccel.clear()
        batch = ArrayList(64)
        prevGyro = null
        lastGyro = null
        gyroMagnitude = 0f
    }

    private val flushRunnable = object : Runnable {
        override fun run() {
            flush()
            handler?.postDelayed(this, batchIntervalMs)
        }
    }

    private fun flush() {
        if (batch.isEmpty()) return
        val out = batch
        batch = ArrayList(out.size.coerceAtLeast(16))
        onBatch?.invoke(out)
    }

    // ------------------------------------------------------ sensor callbacks

    override fun onSensorChanged(event: SensorEvent) {
        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> onAccel(event)
            Sensor.TYPE_GYROSCOPE -> onGyro(event)
        }
    }

    private fun onAccel(e: SensorEvent) {
        pendingAccel.addLast(AccelEvent(e.timestamp, e.values[0], e.values[1], e.values[2]))

        if (gyroscope == null) {
            // Nothing to wait for; emit immediately with zero angular rate.
            drainAll(0f, 0f, 0f)
            return
        }
        // A stalled gyroscope must not grow this queue without bound. Emitting
        // with a held reading is worse than interpolating but far better than
        // an OOM, and it is logged so the cause is visible.
        if (pendingAccel.size > MAX_PENDING) {
            Log.w(TAG, "gyroscope stalled; flushing ${pendingAccel.size} held samples")
            val g = lastGyro
            drainAll(g?.x ?: 0f, g?.y ?: 0f, g?.z ?: 0f)
        }
    }

    private fun onGyro(e: SensorEvent) {
        val g = GyroEvent(e.timestamp, e.values[0], e.values[1], e.values[2])
        prevGyro = lastGyro
        lastGyro = g
        gyroMagnitude = sqrt(g.x * g.x + g.y * g.y + g.z * g.z)

        val older = prevGyro ?: return   // need two before we can interpolate
        emitBracketed(older, g)
    }

    /**
     * Emits every held accelerometer sample whose timestamp falls at or before
     * [newer], interpolating the gyroscope across the [older]..[newer] span.
     */
    private fun emitBracketed(older: GyroEvent, newer: GyroEvent) {
        val span = (newer.t - older.t).toDouble()
        while (true) {
            val a = pendingAccel.firstOrNull() ?: return
            if (a.t > newer.t) return
            pendingAccel.removeFirst()

            // alpha 0 -> older, 1 -> newer. Clamped so an accelerometer sample
            // that predates the older gyro reading holds rather than
            // extrapolating backwards off the end of the span.
            val alpha = if (span > 0.0) {
                ((a.t - older.t) / span).coerceIn(0.0, 1.0).toFloat()
            } else 1f

            add(
                a,
                older.x + (newer.x - older.x) * alpha,
                older.y + (newer.y - older.y) * alpha,
                older.z + (newer.z - older.z) * alpha
            )
        }
    }

    private fun drainAll(gx: Float, gy: Float, gz: Float) {
        while (pendingAccel.isNotEmpty()) add(pendingAccel.removeFirst(), gx, gy, gz)
    }

    private fun add(a: AccelEvent, gx: Float, gy: Float, gz: Float) {
        // SensorEvent.timestamp is nanoseconds on the monotonic since-boot
        // clock, which is exactly what the wire format specifies. Note it is a
        // different clock from the frame timestamps; see Protocol.encodeImuBatch.
        batch.add(ImuSample(a.t, a.x, a.y, a.z, gx, gy, gz))
        totalSamples.incrementAndGet()
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
        if (accuracy == SensorManager.SENSOR_STATUS_UNRELIABLE) {
            Log.w(TAG, "${sensor?.name} reports UNRELIABLE accuracy")
        }
    }
}
