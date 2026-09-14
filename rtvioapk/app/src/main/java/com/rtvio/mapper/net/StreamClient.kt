package com.rtvio.mapper.net

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, ERROR }

data class ConnectionInfo(
    val state: ConnectionState = ConnectionState.DISCONNECTED,
    val host: String = "",
    val port: Int = 0,
    /** Human-readable detail: the error, or the handshake result. */
    val detail: String = "",
    val attempt: Int = 0,
    /** Protocol version the desktop greeted with; 0 if it sent no handshake. */
    val serverVersion: Int = 0
) {
    /** The desktop is rtvio.studio: wait for its START/STOP instead of streaming. */
    val remoteControl: Boolean
        get() = state == ConnectionState.CONNECTED && serverVersion >= Protocol.VERSION_REMOTE_CONTROL
}

data class StreamStats(
    val framesSent: Long = 0,
    val framesDropped: Long = 0,
    val imuBatchesSent: Long = 0,
    val imuSamplesSent: Long = 0,
    val gpsFixesSent: Long = 0,
    val bytesSent: Long = 0,
    /** Throughput actually pushed onto the socket over the last second. */
    val mbps: Double = 0.0,
    /**
     * Milliseconds between a live frame being handed to this client and its
     * last byte reaching the socket (spooled recording frames are not timed -
     * waiting is their whole point).
     */
    val latencyMs: Double = 0.0,
    val videoQueueDepth: Int = 0,
    val videoQueueCapacity: Int = 0,
    /** Recording frames waiting in the on-disk spool. */
    val spoolPending: Int = 0,
    val spoolBytes: Long = 0,
    val elapsedMs: Long = 0
)

/**
 * Owns the TCP connection to the desktop receiver and everything that goes over
 * it.
 *
 * - **One socket, one writer.** Video, IMU, GPS and status are multiplexed onto
 *   a single stream because the desktop reader dispatches on a leading header
 *   byte. A single writer coroutine guarantees packets are never interleaved.
 *
 * - **Control traffic outranks video.** IMU, GPS and status packets are drained
 *   before video on every pass. They are tiny and irreplaceable; a frame is not.
 *
 * - **Two policies for frames.** Live streaming (a v1 receiver) uses a bounded
 *   queue that drops its *oldest* frame when full - stale frames are worthless
 *   to a live view. A recording ([beginRecording]) appends every frame to a
 *   [FrameSpool] on flash instead and never drops one; the writer drains it in
 *   order, through WiFi stalls and reconnects, and keeps going after the camera
 *   stops until the backlog is gone.
 *
 * - **Blocking writes are the backpressure.** A congested socket blocks in
 *   write(); that is intended. Closing the socket from [stop] unblocks it with
 *   an IOException, which is how shutdown and reconnect both work.
 *
 * - **Commands (v2).** When the desktop greets with protocol v2, a reader
 *   coroutine delivers each COMMAND packet's JSON to [onCommand].
 */
class StreamClient(
    private val videoCapacity: Int = 10,
    private val controlCapacity: Int = 512
) {

    private enum class Kind { FRAME, IMU, GPS, META }

    private class Packet(
        val bytes: ByteArray,
        val kind: Kind,
        val enqueuedNs: Long,
        /** IMU sample count, so stats can be credited without re-parsing. */
        val units: Int = 1
    )

    private val videoQueue = ArrayBlockingQueue<Packet>(videoCapacity)
    private val controlQueue = ArrayBlockingQueue<Packet>(controlCapacity)
    /** Latest-only viewfinder image; a newer one simply replaces it. */
    private val previewSlot = AtomicReference<ByteArray?>(null)

    @Volatile private var spool: FrameSpool? = null
    private val spoolFramesSent = AtomicLong()

    /** Invoked on an IO thread with each desktop command's JSON text (v2 only). */
    @Volatile var onCommand: ((String) -> Unit)? = null

    private val _connection = MutableStateFlow(ConnectionInfo())
    val connection: StateFlow<ConnectionInfo> = _connection.asStateFlow()

    private val _stats = MutableStateFlow(StreamStats())
    val stats: StateFlow<StreamStats> = _stats.asStateFlow()

    private val framesSent = AtomicLong()
    private val framesDropped = AtomicLong()
    private val imuBatchesSent = AtomicLong()
    private val imuSamplesSent = AtomicLong()
    private val gpsFixesSent = AtomicLong()
    private val bytesSent = AtomicLong()
    private val latencyNs = AtomicLong()
    private val latencyCount = AtomicLong()

    @Volatile private var socket: Socket? = null
    @Volatile private var running = false
    private var startedAtMs = 0L
    private var scope: CoroutineScope? = null

    val isRunning: Boolean get() = running

    // ------------------------------------------------------------ lifecycle

    fun start(host: String, port: Int, autoReconnect: Boolean, baseBackoffSec: Int) {
        if (running) return
        running = true
        startedAtMs = System.currentTimeMillis()
        resetCounters()

        val s = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = s
        s.launch { connectionLoop(host, port, autoReconnect, baseBackoffSec * 1000L) }
        s.launch { statsTicker() }
    }

    fun stop() {
        if (!running) return
        running = false
        closeSocketQuietly()
        scope?.cancel()
        scope = null
        videoQueue.clear()
        controlQueue.clear()
        previewSlot.set(null)
        _connection.value = _connection.value.copy(
            state = ConnectionState.DISCONNECTED,
            detail = "stopped"
        )
        publishStats()
    }

    private fun resetCounters() {
        framesSent.set(0); framesDropped.set(0)
        imuBatchesSent.set(0); imuSamplesSent.set(0); gpsFixesSent.set(0)
        bytesSent.set(0); latencyNs.set(0); latencyCount.set(0)
        _stats.value = StreamStats(videoQueueCapacity = videoCapacity)
    }

    // ------------------------------------------------------------ recording

    /** From now on every frame goes to [s] and is never dropped. */
    fun beginRecording(s: FrameSpool) {
        spoolFramesSent.set(0)
        spool = s
    }

    /**
     * Detaches the spool once it is drained (the caller checks
     * [recordingBacklog] first) and returns it for closing.
     */
    fun finishRecording(): FrameSpool? {
        val s = spool
        spool = null
        return s
    }

    val isRecording: Boolean get() = spool != null
    val recordingBacklog: Int get() = spool?.pending ?: 0
    val recordingBacklogBytes: Long get() = spool?.pendingBytes ?: 0L
    /** Frames of the current recording written to the socket so far. */
    val recordingFramesSent: Long get() = spoolFramesSent.get()
    val recordingFramesRefused: Long get() = spool?.refused ?: 0L
    val spoolFreeBytes: Long get() = spool?.freeBytes ?: 0L

    // ------------------------------------------------------------- ingestion

    /**
     * Hands a JPEG frame to the sender. Never blocks for long: a recording
     * appends it to the spool (one sequential flash write), live streaming
     * queues it and evicts the oldest frame if the queue is full.
     *
     * [jpeg] is read synchronously into the packet and not retained, so the
     * caller is free to hand over a reusable encoder buffer.
     *
     * @return false if the frame was dropped.
     */
    fun offerFrame(
        timestampMs: Long,
        width: Int,
        height: Int,
        jpeg: ByteArray,
        jpegLength: Int = jpeg.size
    ): Boolean {
        val bytes = Protocol.encodeFrame(timestampMs, width, height, jpeg, jpegLength)
        val sp = spool
        if (sp != null) {
            val ok = sp.append(bytes)
            if (!ok) framesDropped.incrementAndGet()
            return ok
        }
        val packet = Packet(bytes, Kind.FRAME, System.nanoTime())
        if (videoQueue.offer(packet)) return true
        // Full: evict the oldest frame and take its place. Losing the stale one
        // is strictly better than losing the fresh one.
        videoQueue.poll()
        framesDropped.incrementAndGet()
        return videoQueue.offer(packet)
    }

    /** Viewfinder image for the desktop while armed (v2). Latest wins. */
    fun offerPreview(timestampMs: Long, width: Int, height: Int, jpeg: ByteArray, jpegLength: Int) {
        previewSlot.set(
            Protocol.encodeFrame(timestampMs, width, height, jpeg, jpegLength, Protocol.HEADER_PREVIEW)
        )
    }

    /** Queues an IMU batch. Control traffic is never dropped unless truly saturated. */
    fun offerImuBatch(samples: List<ImuSample>) {
        if (samples.isEmpty()) return
        enqueueControl(
            Packet(Protocol.encodeImuBatch(samples), Kind.IMU, System.nanoTime(), samples.size)
        )
    }

    fun offerGps(
        timestampMs: Long,
        latitude: Double,
        longitude: Double,
        altitudeM: Float,
        accuracyM: Float
    ) {
        enqueueControl(
            Packet(
                Protocol.encodeGps(timestampMs, latitude, longitude, altitudeM, accuracyM),
                Kind.GPS,
                System.nanoTime()
            )
        )
    }

    /** Phone state for rtvio.studio (v2 only - a v1 receiver would not parse it). */
    fun offerStatus(json: String) {
        if (!_connection.value.remoteControl) return
        enqueueControl(Packet(Protocol.encodeStatus(json), Kind.META, System.nanoTime()))
    }

    /**
     * Sends camera intrinsics. Sent at connect and at each recording start,
     * once the frame size is known.
     */
    fun offerIntrinsics(
        fxPix: Float,
        fyPix: Float,
        cxPix: Float,
        cyPix: Float,
        k1: Double = 0.0,
        k2: Double = 0.0,
        p1: Double = 0.0,
        p2: Double = 0.0,
        k3: Double = 0.0,
        source: String = ""
    ) {
        enqueueControl(
            Packet(
                Protocol.encodeIntrinsics(fxPix, fyPix, cxPix, cyPix, k1, k2, p1, p2, k3, source),
                Kind.META,
                System.nanoTime()
            )
        )
    }

    private fun enqueueControl(p: Packet) {
        if (controlQueue.offer(p)) return
        controlQueue.poll()
        controlQueue.offer(p)
    }

    // -------------------------------------------------------- connection loop

    private suspend fun connectionLoop(
        host: String,
        port: Int,
        autoReconnect: Boolean,
        baseBackoffMs: Long
    ) {
        var backoff = baseBackoffMs
        var attempt = 0

        while (running && (scope?.isActive == true)) {
            attempt++
            _connection.value = ConnectionInfo(ConnectionState.CONNECTING, host, port, "", attempt)
            var sock: Socket? = null
            try {
                sock = Socket().apply {
                    tcpNoDelay = true          // frames are latency-sensitive
                    keepAlive = true
                    sendBufferSize = SEND_BUFFER_BYTES
                    connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
                }
                socket = sock

                val (handshakeDetail, version) = readGreeting(sock)
                backoff = baseBackoffMs      // a good connection resets the backoff
                if (version >= Protocol.VERSION_REMOTE_CONTROL) startCommandReader(sock)
                _connection.value = ConnectionInfo(
                    ConnectionState.CONNECTED, host, port, handshakeDetail, attempt, version
                )

                // Blocks here for the life of the connection.
                writeLoop(BufferedOutputStream(sock.getOutputStream(), SEND_BUFFER_BYTES))
            } catch (e: Exception) {
                if (!running) break
                Log.w(TAG, "connection to $host:$port failed", e)
                _connection.value = ConnectionInfo(
                    ConnectionState.ERROR, host, port,
                    e.message ?: e.javaClass.simpleName, attempt
                )
            } finally {
                sock?.let { closeQuietly(it) }
                if (socket === sock) socket = null
            }

            if (!running) break
            if (!autoReconnect && spool == null) {
                _connection.value = _connection.value.copy(
                    state = ConnectionState.DISCONNECTED,
                    detail = "auto-reconnect is off"
                )
                running = false
                break
            }
            // A recording always reconnects: its spooled frames still have to
            // reach the desktop, whatever the auto-reconnect preference says.
            val wait = if (spool != null) minOf(backoff, 2_000L) else backoff
            _connection.value = _connection.value.copy(detail = "retrying in ${wait / 1000}s")
            delay(wait)
            backoff = (backoff * 2).coerceAtMost(MAX_BACKOFF_MS)
        }
    }

    /**
     * Reads the desktop's optional 0xAA greeting. A receiver that just starts
     * reading without greeting us is still a working (v1) receiver.
     */
    private fun readGreeting(sock: Socket): Pair<String, Int> = try {
        sock.soTimeout = HANDSHAKE_TIMEOUT_MS
        val ack = Protocol.readHandshakeAck(DataInputStream(sock.getInputStream()))
        sock.soTimeout = 0
        when {
            ack == null -> "connected (no handshake sent)" to 0
            !ack.ok -> "connected, server reported status ${ack.status}" to ack.protocolVersion
            ack.protocolVersion >= Protocol.VERSION_REMOTE_CONTROL ->
                "connected to RTVIO Studio (protocol v${ack.protocolVersion})" to ack.protocolVersion
            else -> "connected, protocol v${ack.protocolVersion} (live streaming)" to ack.protocolVersion
        }
    } catch (e: IOException) {
        try {
            sock.soTimeout = 0
        } catch (ignored: IOException) {
            // Socket already dead; the write loop will surface it.
        }
        "connected (no handshake within ${HANDSHAKE_TIMEOUT_MS}ms)" to 0
    }

    private fun startCommandReader(sock: Socket) {
        scope?.launch {
            val input = DataInputStream(BufferedInputStream(sock.getInputStream()))
            try {
                while (isActive) {
                    val json = Protocol.readCommand(input)
                    try {
                        onCommand?.invoke(json)
                    } catch (e: Exception) {
                        Log.w(TAG, "command handler failed for $json", e)
                    }
                }
            } catch (e: IOException) {
                // The socket closed (stop, reconnect) or the stream went out of
                // sync; closing makes the write loop fail and reconnect.
                if (running) Log.w(TAG, "command reader ended: ${e.message}")
                closeQuietly(sock)
            }
        }
    }

    /** Drains everything onto the socket until the connection dies or we stop. */
    private fun writeLoop(out: OutputStream) {
        out.use { stream ->
            while (running && (scope?.isActive == true)) {
                val control = controlQueue.poll()
                if (control != null) {
                    stream.write(control.bytes)
                    credit(control)
                    continue
                }
                val spooled = spool?.poll()
                if (spooled != null) {
                    stream.write(spooled)
                    bytesSent.addAndGet(spooled.size.toLong())
                    framesSent.incrementAndGet()
                    spoolFramesSent.incrementAndGet()
                    continue
                }
                val preview = previewSlot.getAndSet(null)
                if (preview != null) {
                    stream.write(preview)
                    bytesSent.addAndGet(preview.size.toLong())
                    stream.flush()
                    continue
                }
                // Nothing urgent: flush what was written, then wait briefly on
                // the live video queue. The short timeout is what picks up new
                // spool/preview/control work without busy-spinning.
                stream.flush()
                val packet = videoQueue.poll(20, TimeUnit.MILLISECONDS) ?: continue
                stream.write(packet.bytes)
                credit(packet)
            }
            stream.flush()
        }
    }

    private fun credit(p: Packet) {
        bytesSent.addAndGet(p.bytes.size.toLong())
        when (p.kind) {
            Kind.FRAME -> {
                framesSent.incrementAndGet()
                latencyNs.addAndGet(System.nanoTime() - p.enqueuedNs)
                latencyCount.incrementAndGet()
            }
            Kind.IMU -> {
                imuBatchesSent.incrementAndGet()
                imuSamplesSent.addAndGet(p.units.toLong())
            }
            Kind.GPS -> gpsFixesSent.incrementAndGet()
            Kind.META -> Unit
        }
    }

    // ------------------------------------------------------------------ stats

    private suspend fun statsTicker() {
        var lastBytes = 0L
        var lastAtNs = System.nanoTime()
        while (scope?.isActive == true) {
            delay(1000)
            val nowNs = System.nanoTime()
            val now = bytesSent.get()
            val elapsedSec = (nowNs - lastAtNs) / 1e9
            val mbps = if (elapsedSec > 0) (now - lastBytes) * 8.0 / 1e6 / elapsedSec else 0.0
            lastBytes = now
            lastAtNs = nowNs

            val n = latencyCount.getAndSet(0)
            val totalNs = latencyNs.getAndSet(0)
            val latMs = if (n > 0) totalNs / n / 1e6 else _stats.value.latencyMs

            publishStats(mbps, latMs)
        }
    }

    private fun publishStats(mbps: Double = 0.0, latencyMs: Double = 0.0) {
        _stats.value = StreamStats(
            framesSent = framesSent.get(),
            framesDropped = framesDropped.get(),
            imuBatchesSent = imuBatchesSent.get(),
            imuSamplesSent = imuSamplesSent.get(),
            gpsFixesSent = gpsFixesSent.get(),
            bytesSent = bytesSent.get(),
            mbps = mbps,
            latencyMs = latencyMs,
            videoQueueDepth = videoQueue.size,
            videoQueueCapacity = videoCapacity,
            spoolPending = recordingBacklog,
            spoolBytes = recordingBacklogBytes,
            elapsedMs = if (startedAtMs == 0L) 0 else System.currentTimeMillis() - startedAtMs
        )
    }

    // ------------------------------------------------------------------ utils

    private fun closeSocketQuietly() = socket?.let { closeQuietly(it) }

    private fun closeQuietly(s: Socket) {
        try {
            s.close()
        } catch (e: IOException) {
            // Closing a socket that is already broken is the normal path here.
        }
    }

    companion object {
        private const val TAG = "StreamClient"
        private const val CONNECT_TIMEOUT_MS = 5_000

        /** How long to wait for the desktop's 0xAA greeting before moving on. */
        private const val HANDSHAKE_TIMEOUT_MS = 1_500

        private const val MAX_BACKOFF_MS = 60_000L
        private const val SEND_BUFFER_BYTES = 256 * 1024

        /**
         * One-shot reachability check for the settings screen's Test button.
         *
         * Connects, waits for the greeting, then disconnects without sending
         * anything. Returns a human-readable result either way.
         */
        suspend fun testConnection(host: String, port: Int): Result<String> =
            withContext(Dispatchers.IO) {
                val startNs = System.nanoTime()
                try {
                    Socket().use { s ->
                        s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
                        val connectMs = (System.nanoTime() - startNs) / 1e6
                        s.soTimeout = HANDSHAKE_TIMEOUT_MS
                        val ack = try {
                            Protocol.readHandshakeAck(DataInputStream(s.getInputStream()))
                        } catch (e: IOException) {
                            null
                        }
                        val note = when {
                            ack == null -> "no 0xAA handshake (receiver may not send one)"
                            !ack.ok -> "handshake status ${ack.status} (error)"
                            ack.protocolVersion >= Protocol.VERSION_REMOTE_CONTROL ->
                                "RTVIO Studio, remote control available"
                            else -> "handshake OK, protocol v${ack.protocolVersion}"
                        }
                        Result.success("Connected in %.0f ms - %s".format(connectMs, note))
                    }
                } catch (e: Exception) {
                    Result.failure(e)
                }
            }
    }
}
