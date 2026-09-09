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
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, ERROR }

data class ConnectionInfo(
    val state: ConnectionState = ConnectionState.DISCONNECTED,
    val host: String = "",
    val port: Int = 0,
    /** Human-readable detail: the error, or the handshake result. */
    val detail: String = "",
    val attempt: Int = 0
)

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
     * Milliseconds between a frame being handed to this client and its last
     * byte reaching the socket. See the note on [latencyNs] for why this, and
     * not an ICMP ping, is the number reported.
     */
    val latencyMs: Double = 0.0,
    val videoQueueDepth: Int = 0,
    val videoQueueCapacity: Int = 0,
    val elapsedMs: Long = 0
)

/**
 * Owns the TCP connection to the desktop receiver and everything that goes over
 * it.
 *
 * Design notes worth knowing before changing this file:
 *
 * - **One socket, one writer.** Video, IMU and GPS are multiplexed onto a single
 *   stream because the desktop reader dispatches on a leading header byte. A
 *   single writer coroutine guarantees packets are never interleaved.
 *
 * - **Control traffic outranks video.** IMU and GPS packets are drained before
 *   video on every pass. They are tiny and irreplaceable; a frame is neither.
 *
 * - **Video drops, control waits.** The video queue is bounded at
 *   [videoCapacity] and drops its *oldest* entry when full, so congestion costs
 *   us stale frames rather than latency. This is the spec's section 10.1.
 *
 * - **Blocking writes are the backpressure.** A congested socket blocks in
 *   write(); that is intended. Closing the socket from [stop] unblocks it with
 *   an IOException, which is how shutdown and reconnect both work.
 */
class StreamClient(
    private val videoCapacity: Int = 10,
    private val controlCapacity: Int = 512
) {

    // Kotlin permits exactly one companion object per class; the shared
    // constants and the standalone probe both live in the one at the bottom.

    private enum class Kind { FRAME, IMU, GPS }

    private class Packet(
        val bytes: ByteArray,
        val kind: Kind,
        val enqueuedNs: Long,
        /** IMU sample count, so stats can be credited without re-parsing. */
        val units: Int = 1
    )

    private val videoQueue = ArrayBlockingQueue<Packet>(videoCapacity)
    private val controlQueue = ArrayBlockingQueue<Packet>(controlCapacity)

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

    /**
     * Sum and count of per-frame queue-to-wire times over the current second.
     *
     * The spec asks for a "ping". A round-trip ping is not measurable on this
     * protocol: the desktop speaks exactly once, at connect, and adding a
     * heartbeat packet would break the receiver in section 14. Opening a
     * throwaway TCP connection each second to time the handshake would make a
     * single-client receiver see a phantom second client. So the number
     * reported is the one that is both measurable and more actionable for a
     * live pipeline: how long a frame takes to get from capture to the wire.
     * It rises exactly when the link is the bottleneck, which is what the
     * operator needs to see.
     */
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

    // ------------------------------------------------------------- ingestion

    /**
     * Hands a JPEG frame to the sender. Never blocks.
     *
     * [jpeg] is read synchronously into the packet and not retained, so the
     * caller is free to hand over a reusable encoder buffer. [jpegLength] lets
     * an over-allocated buffer be passed without a defensive copy first.
     *
     * @return false if the frame was dropped because the link could not keep up.
     */
    fun offerFrame(
        timestampMs: Long,
        width: Int,
        height: Int,
        jpeg: ByteArray,
        jpegLength: Int = jpeg.size
    ): Boolean {
        val packet = Packet(
            Protocol.encodeFrame(timestampMs, width, height, jpeg, jpegLength),
            Kind.FRAME,
            System.nanoTime()
        )
        if (videoQueue.offer(packet)) return true
        // Full: evict the oldest frame and take its place. Losing the stale one
        // is strictly better than losing the fresh one.
        videoQueue.poll()
        framesDropped.incrementAndGet()
        return videoQueue.offer(packet)
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

    /**
     * Sends camera intrinsics. Called once at session start, right after the
     * desktop handshake. These are the ground truth for the device's camera
     * and must be used in preference to any cached or synthetic defaults.
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
                Kind.IMU,  // Kind.IMU is just for the enum; only FRAME goes to videoQueue
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

                val handshakeDetail = readGreeting(sock)
                backoff = baseBackoffMs      // a good connection resets the backoff
                _connection.value =
                    ConnectionInfo(ConnectionState.CONNECTED, host, port, handshakeDetail, attempt)

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
            if (!autoReconnect) {
                _connection.value = _connection.value.copy(
                    state = ConnectionState.DISCONNECTED,
                    detail = "auto-reconnect is off"
                )
                running = false
                break
            }
            // Exponential backoff, capped: 5s, 10s, 20s, 40s, 60s, 60s ...
            _connection.value = _connection.value.copy(
                detail = "retrying in ${backoff / 1000}s"
            )
            delay(backoff)
            backoff = (backoff * 2).coerceAtMost(MAX_BACKOFF_MS)
        }
    }

    /**
     * Reads the desktop's optional 0xAA greeting.
     *
     * A receiver that just starts reading without greeting us is still a working
     * receiver, so a missing or malformed ack is reported, not treated as a
     * failure.
     */
    private fun readGreeting(sock: Socket): String = try {
        sock.soTimeout = HANDSHAKE_TIMEOUT_MS
        val ack = Protocol.readHandshakeAck(DataInputStream(sock.getInputStream()))
        sock.soTimeout = 0
        when {
            ack == null -> "connected (no handshake sent)"
            !ack.ok -> "connected, server reported status ${ack.status}"
            ack.protocolVersion != Protocol.VERSION ->
                "connected, protocol v${ack.protocolVersion} (app speaks v${Protocol.VERSION})"
            else -> "connected, protocol v${ack.protocolVersion}"
        }
    } catch (e: IOException) {
        try {
            sock.soTimeout = 0
        } catch (ignored: IOException) {
            // Socket already dead; the write loop will surface it.
        }
        "connected (no handshake within ${HANDSHAKE_TIMEOUT_MS}ms)"
    }

    /** Drains both queues onto the socket until the connection dies or we stop. */
    private fun writeLoop(out: OutputStream) {
        out.use { stream ->
            while (running && (scope?.isActive == true)) {
                // Control first: IMU and GPS are small and cannot be regenerated.
                // The bounded wait on the video queue keeps this loop responsive
                // to stop() without busy-spinning when there is nothing to send.
                val packet = controlQueue.poll()
                    ?: videoQueue.poll(100, TimeUnit.MILLISECONDS)
                if (packet == null) {
                    stream.flush()
                    continue
                }

                stream.write(packet.bytes)
                if (controlQueue.isEmpty() && videoQueue.isEmpty()) stream.flush()

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
