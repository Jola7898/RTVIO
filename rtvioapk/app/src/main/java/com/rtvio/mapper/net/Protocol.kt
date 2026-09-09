package com.rtvio.mapper.net

import java.io.DataInputStream
import java.io.EOFException
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * The RTVIO wire format.
 *
 * Every multi-byte field is big-endian (network byte order), which is also
 * java.nio's default, so no explicit ordering call is strictly required - it is
 * stated anyway so the intent survives a future refactor.
 *
 * All three payload types are multiplexed onto one TCP stream. A reader
 * dispatches on the first byte of each packet; because a single writer thread
 * emits whole packets atomically, a frame is never interleaved with an IMU
 * batch.
 */
object Protocol {

    const val VERSION = 1

    const val HEADER_FRAME: Byte = 0xFF.toByte()
    const val HEADER_IMU: Byte = 0xFE.toByte()
    const val HEADER_GPS: Byte = 0xFD.toByte()
    const val HEADER_INTRINSICS: Byte = 0xFC.toByte()
    const val HEADER_HANDSHAKE_ACK: Byte = 0xAA.toByte()

    /** Bytes a frame packet costs on top of the JPEG payload. */
    const val FRAME_OVERHEAD = 1 + 8 + 4 + 4 + 4      // 21
    /** Bytes an IMU batch costs on top of its samples. */
    const val IMU_OVERHEAD = 1 + 2                    // 3
    /** Bytes one IMU sample occupies. */
    const val IMU_SAMPLE_SIZE = 8 + 4 * 6             // 32
    /** Total size of a GPS packet. */
    const val GPS_PACKET_SIZE = 1 + 8 + 8 + 8 + 4 + 4 // 33
    /** Total size of the handshake ack the desktop sends on connect. */
    const val HANDSHAKE_ACK_SIZE = 1 + 4 + 1          // 6
    /**
     * Intrinsics packet: 0xFC + 4 floats (fx, fy, cx, cy) + 5 doubles (k1, k2, p1, p2, k3)
     *                  = 1 + 16 + 40 = 57 bytes, plus a null-terminated source string
     */

    /**
     * struct FramePacket { u8 0xFF; i64 timestamp_ms; i32 w; i32 h; i32 size; u8 jpeg[size]; }
     *
     * [timestampMs] is wall-clock (System.currentTimeMillis()), matching the
     * spec. Note this is a *different clock* from the IMU timestamps - see
     * [encodeImuBatch].
     */
    fun encodeFrame(
        timestampMs: Long,
        width: Int,
        height: Int,
        jpeg: ByteArray,
        jpegLength: Int = jpeg.size
    ): ByteArray {
        val buf = ByteBuffer.allocate(FRAME_OVERHEAD + jpegLength).order(ByteOrder.BIG_ENDIAN)
        buf.put(HEADER_FRAME)
        buf.putLong(timestampMs)
        buf.putInt(width)
        buf.putInt(height)
        buf.putInt(jpegLength)
        buf.put(jpeg, 0, jpegLength)
        return buf.array()
    }

    /**
     * struct IMUBatchPacket { u8 0xFE; i16 count; { i64 t_ns; f32 a[3]; f32 g[3]; }[count]; }
     *
     * Sample timestamps are SensorEvent.timestamp - nanoseconds since boot, on
     * the monotonic clock, NOT epoch. That is what the spec asks for and it is
     * the right choice (it does not jump when NTP corrects the wall clock), but
     * it means the desktop must recover the offset between the two clocks
     * before it can associate an IMU sample with a frame. See README
     * "Clock domains" for how to do that from the stream alone.
     *
     * @param count how many entries of [samples] are populated; the array may
     *   be a reusable over-allocated scratch buffer.
     */
    fun encodeImuBatch(samples: List<ImuSample>, count: Int = samples.size): ByteArray {
        require(count <= Short.MAX_VALUE) { "IMU batch of $count exceeds the i16 count field" }
        val buf = ByteBuffer.allocate(IMU_OVERHEAD + count * IMU_SAMPLE_SIZE)
            .order(ByteOrder.BIG_ENDIAN)
        buf.put(HEADER_IMU)
        buf.putShort(count.toShort())
        for (i in 0 until count) {
            val s = samples[i]
            buf.putLong(s.timestampNs)
            buf.putFloat(s.ax); buf.putFloat(s.ay); buf.putFloat(s.az)
            buf.putFloat(s.gx); buf.putFloat(s.gy); buf.putFloat(s.gz)
        }
        return buf.array()
    }

    /**
     * struct GPSPacket { u8 0xFD; i64 t_ms; f64 lat; f64 lon; f32 alt; f32 acc; }
     *
     * [timestampMs] is Location.getTime(), i.e. epoch milliseconds from the GNSS
     * fix itself - the same clock domain as the frame timestamps.
     */
    fun encodeGps(
        timestampMs: Long,
        latitude: Double,
        longitude: Double,
        altitudeM: Float,
        accuracyM: Float
    ): ByteArray {
        val buf = ByteBuffer.allocate(GPS_PACKET_SIZE).order(ByteOrder.BIG_ENDIAN)
        buf.put(HEADER_GPS)
        buf.putLong(timestampMs)
        buf.putDouble(latitude)
        buf.putDouble(longitude)
        buf.putFloat(altitudeM)
        buf.putFloat(accuracyM)
        return buf.array()
    }

    /**
     * struct IntrinsicsPacket {
     *   u8 0xFC;
     *   f32 fx_pix, fy_pix, cx_pix, cy_pix;
     *   f64 k1, k2, p1, p2, k3;
     *   u16 source_len;
     *   u8 source[source_len];  // null-terminated string describing the source
     * }
     *
     * Sent once at session start, right after the phone connects. Contains the
     * camera intrinsic matrix K and distortion coefficients. Always preferred over
     * any cached calibration or hardcoded defaults on the desktop.
     */
    fun encodeIntrinsics(
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
    ): ByteArray {
        val sourceBytes = source.toByteArray(Charsets.UTF_8)
        val buf = ByteBuffer.allocate(1 + 16 + 40 + 2 + sourceBytes.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(HEADER_INTRINSICS)
        buf.putFloat(fxPix)
        buf.putFloat(fyPix)
        buf.putFloat(cxPix)
        buf.putFloat(cyPix)
        buf.putDouble(k1)
        buf.putDouble(k2)
        buf.putDouble(p1)
        buf.putDouble(p2)
        buf.putDouble(k3)
        buf.putShort(sourceBytes.size.toShort())
        buf.put(sourceBytes)
        return buf.array()
    }

    /** Result of reading the desktop's greeting. */
    data class HandshakeAck(val protocolVersion: Int, val status: Int) {
        val ok: Boolean get() = status == 0
    }

    /**
     * Reads the 6-byte ack the desktop is specified to send on connect.
     *
     * Returns null if the peer sent something that is not an ack, or closed
     * without sending one. A null is deliberately not fatal: a receiver that
     * simply starts reading without greeting us is still a usable receiver, so
     * the caller reports "no handshake" rather than refusing to stream.
     */
    fun readHandshakeAck(input: DataInputStream): HandshakeAck? = try {
        val header = input.readByte()
        if (header != HEADER_HANDSHAKE_ACK) {
            null
        } else {
            // uint32 read into a Long so 0x80000000+ does not come back negative.
            val version = input.readInt().toLong() and 0xFFFFFFFFL
            val status = input.readByte().toInt() and 0xFF
            HandshakeAck(version.toInt(), status)
        }
    } catch (e: EOFException) {
        null
    }
}

/** One time-aligned inertial sample: accelerometer m/s^2, gyroscope rad/s. */
data class ImuSample(
    val timestampNs: Long,
    val ax: Float, val ay: Float, val az: Float,
    val gx: Float, val gy: Float, val gz: Float
)
