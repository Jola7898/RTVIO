package com.rtvio.mapper.capture

import android.location.Location
import android.os.SystemClock
import android.util.Log
import com.rtvio.mapper.data.CameraIntrinsics
import com.rtvio.mapper.net.ImuSample
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicLong

/**
 * Fully offline capture to the phone's own storage - no socket, no ARMED
 * receiver, no WiFi required at all. Written in the exact layout
 * `rtvio.vggt_reconstruct --from-recording` reads (`frames/%06d.jpg` +
 * `frame_timestamps.json`, `gps_data.json`, mirroring the desktop's
 * `stream/recorder.py` `SessionRecorder`), so a session captured with no PC in
 * reach at all reconstructs identically to one that streamed live, once
 * copied or transferred ([com.rtvio.mapper.net.SessionTransferClient]) over.
 *
 * JPEG writes happen on their own thread behind a bounded queue, same
 * rationale as the desktop recorder: a slow SD card must cost a dropped frame
 * (recorded as a `null` timestamp, same convention `_load_recording_frames`
 * already understands), never stall the camera pipeline.
 */
class LocalSessionRecorder(private val sessionDir: File) {

    companion object {
        private const val TAG = "LocalSessionRecorder"
        private const val QUEUE_CAPACITY = 256
        private val POISON = Runnable {}
    }

    data class Summary(
        val sessionId: String,
        val dir: File,
        val durationMs: Long,
        val frames: Int,
        val framesDropped: Int,
        val imuSamples: Int,
        val gpsFixes: Int,
        val bytesWritten: Long
    )

    private val framesDir = File(sessionDir, "frames")
    private val startWallMs = System.currentTimeMillis()
    private val startElapsedNs = SystemClock.elapsedRealtimeNanos()

    private val lock = Any()
    private var frameIndex = 0
    private var framesDropped = 0
    private val bytesWritten = AtomicLong()
    private val frameTimes = mutableListOf<Double?>()
    private val imu = JSONArray()
    private val gps = JSONArray()
    private var intrinsics: JSONObject? = null

    private val queue = ArrayBlockingQueue<Runnable>(QUEUE_CAPACITY)
    private val writer = Thread({
        while (true) {
            val job = queue.take()
            if (job === POISON) return@Thread
            job.run()
        }
    }, "rtvio-local-recorder")

    init {
        if (!framesDir.mkdirs() && !framesDir.isDirectory) {
            throw java.io.IOException("could not create $framesDir")
        }
        writer.isDaemon = true
        writer.start()
    }

    val sessionId: String get() = sessionDir.name

    /** Called from the camera's single emitter thread - never concurrently. */
    fun onFrame(jpeg: ByteArray, length: Int, timestampMs: Long) {
        val idx = frameIndex++
        val tSec = round6((timestampMs - startWallMs) / 1000.0)
        val path = File(framesDir, "%06d.jpg".format(idx))
        // Copy now: the caller's encoder buffer is reused for the next frame.
        val copy = jpeg.copyOf(length)
        val accepted = queue.offer(Runnable {
            try {
                path.outputStream().use { it.write(copy) }
                bytesWritten.addAndGet(copy.size.toLong())
            } catch (e: Exception) {
                Log.w(TAG, "frame write failed: ${e.message}")
            }
        })
        synchronized(lock) {
            if (accepted) {
                frameTimes.add(tSec)
            } else {
                framesDropped++
                frameTimes.add(null) // keeps index alignment; the hole is skipped on load
            }
        }
    }

    /** Called from the IMU collector's dedicated HandlerThread. */
    fun onImuBatch(samples: List<ImuSample>) {
        if (samples.isEmpty()) return
        synchronized(lock) {
            for (s in samples) {
                imu.put(JSONObject().apply {
                    put("timestamp", round6((s.timestampNs - startElapsedNs) / 1e9))
                    put("accel_body_xyz", JSONArray(listOf(s.ax, s.ay, s.az)))
                    put("gyro_body_xyz", JSONArray(listOf(s.gx, s.gy, s.gz)))
                })
            }
        }
    }

    /** Called from the GPS collector's dedicated HandlerThread. */
    fun onGpsFix(location: Location) {
        synchronized(lock) {
            gps.put(JSONObject().apply {
                put("timestamp", round6((location.time - startWallMs) / 1000.0))
                put("latitude_deg", location.latitude)
                put("longitude_deg", location.longitude)
                put("altitude_m", location.altitude)
                put("accuracy_m", if (location.hasAccuracy()) location.accuracy.toDouble() else -1.0)
            })
        }
    }

    /** Best-effort; skipped silently if the camera characteristics are not (yet) known. */
    fun setIntrinsics(k: CameraIntrinsics) {
        synchronized(lock) {
            intrinsics = JSONObject().apply {
                put("fx", k.fx_pix); put("fy", k.fy_pix)
                put("cx", k.cx_pix); put("cy", k.cy_pix)
                put("k1", k.k1); put("k2", k.k2)
                put("p1", k.p1); put("p2", k.p2); put("k3", k.k3)
                put("source", k.source)
            }
        }
    }

    /** Drains the writer, dumps the JSON sidecars, and returns the tally. */
    fun finish(measuredFps: Double): Summary {
        queue.put(POISON)
        try {
            writer.join(30_000)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }

        val (times, imuOut, gpsOut, intrinsicsOut, dropped) = synchronized(lock) {
            Quint(frameTimes.toList(), imu, gps, intrinsics, framesDropped)
        }

        writeJson("frame_timestamps.json", JSONArray(times.map { it ?: JSONObject.NULL }))
        writeJson("imu_data.json", imuOut)
        writeJson("gps_data.json", gpsOut)
        intrinsicsOut?.let { writeJson("camera_intrinsics.json", it) }

        val writtenFrames = times.count { it != null }
        val span = times.filterNotNull().let { if (it.size > 1) it.last() - it.first() else 0.0 }
        writeJson("session_meta.json", JSONObject().apply {
            put("frames_written", writtenFrames)
            put("frames_dropped", dropped)
            put("imu_samples", imuOut.length())
            put("gps_fixes", gpsOut.length())
            put("fps", round6(measuredFps))
            put("total_time_s", round6(span))
            put("started_at_wall_ms", startWallMs)
        })

        val bytes = bytesWritten.get()
        val msg = "local session '$sessionId' saved to $sessionDir ($writtenFrames frames, " +
            "${imuOut.length()} imu, ${gpsOut.length()} gps, %.1f MB)".format(bytes / 1e6)
        Log.i(TAG, if (dropped > 0) "$msg - $dropped frames DROPPED by the writer" else msg)

        return Summary(
            sessionId = sessionId,
            dir = sessionDir,
            durationMs = System.currentTimeMillis() - startWallMs,
            frames = writtenFrames,
            framesDropped = dropped,
            imuSamples = imuOut.length(),
            gpsFixes = gpsOut.length(),
            bytesWritten = bytes
        )
    }

    private fun writeJson(name: String, content: Any) {
        try {
            File(sessionDir, name).writeText(content.toString())
        } catch (e: Exception) {
            Log.w(TAG, "could not write $name: ${e.message}")
        }
    }

    private fun round6(v: Double): Double = Math.round(v * 1e6) / 1e6

    private data class Quint<A, B, C, D, E>(val a: A, val b: B, val c: C, val d: D, val e: E)
}
