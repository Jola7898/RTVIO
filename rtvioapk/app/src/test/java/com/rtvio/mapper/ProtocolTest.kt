package com.rtvio.mapper

import com.rtvio.mapper.net.ImuSample
import com.rtvio.mapper.net.Protocol
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.DataInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Byte-level checks on the wire format.
 *
 * These assert the exact layout the desktop receiver unpacks, field by field.
 * A protocol change that is not also a deliberate change to the desktop should
 * fail here rather than in the field, so the assertions are written against
 * literal offsets rather than by re-encoding through the same code.
 */
class ProtocolTest {

    private fun reader(bytes: ByteArray) =
        ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)

    @Test
    fun `frame packet matches the documented layout`() {
        val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x11, 0x22, 0xFF.toByte(), 0xD9.toByte())
        val packet = Protocol.encodeFrame(
            timestampMs = 1_700_000_000_123L, width = 1920, height = 1080, jpeg = jpeg
        )

        assertEquals("total size", Protocol.FRAME_OVERHEAD + jpeg.size, packet.size)

        val b = reader(packet)
        assertEquals("header", 0xFF, b.get().toInt() and 0xFF)
        assertEquals("timestamp_ms", 1_700_000_000_123L, b.long)
        assertEquals("width", 1920, b.int)
        assertEquals("height", 1080, b.int)
        assertEquals("jpeg_size", jpeg.size, b.int)

        val payload = ByteArray(jpeg.size).also { b.get(it) }
        assertTrue("jpeg payload", jpeg.contentEquals(payload))
    }

    /**
     * The encoder accepts an over-allocated buffer plus a length so the camera
     * path never has to copy. Only the first `length` bytes may be sent.
     */
    @Test
    fun `frame packet honours an explicit jpeg length`() {
        val scratch = ByteArray(64) { it.toByte() }
        val packet = Protocol.encodeFrame(1L, 640, 480, scratch, jpegLength = 4)

        assertEquals(Protocol.FRAME_OVERHEAD + 4, packet.size)
        val b = reader(packet)
        b.get(); b.long; b.int; b.int
        assertEquals("jpeg_size reflects the length, not the buffer", 4, b.int)
        assertEquals(0, b.get().toInt())
        assertEquals(1, b.get().toInt())
        assertEquals(2, b.get().toInt())
        assertEquals(3, b.get().toInt())
    }

    @Test
    fun `imu batch matches the documented layout`() {
        val samples = listOf(
            ImuSample(1_000L, 0.1f, 0.2f, 9.81f, 0.01f, -0.02f, 0.03f),
            ImuSample(11_000L, -1.5f, 0f, 9.7f, 0f, 0f, -0.5f)
        )
        val packet = Protocol.encodeImuBatch(samples)

        assertEquals(
            "total size",
            Protocol.IMU_OVERHEAD + samples.size * Protocol.IMU_SAMPLE_SIZE,
            packet.size
        )

        val b = reader(packet)
        assertEquals("header", 0xFE, b.get().toInt() and 0xFF)
        assertEquals("sample_count", 2, b.short.toInt())

        samples.forEach { s ->
            assertEquals(s.timestampNs, b.long)
            assertEquals(s.ax, b.float, 0f)
            assertEquals(s.ay, b.float, 0f)
            assertEquals(s.az, b.float, 0f)
            assertEquals(s.gx, b.float, 0f)
            assertEquals(s.gy, b.float, 0f)
            assertEquals(s.gz, b.float, 0f)
        }
    }

    @Test
    fun `empty imu batch is still well formed`() {
        val packet = Protocol.encodeImuBatch(emptyList())
        assertEquals(Protocol.IMU_OVERHEAD, packet.size)
        val b = reader(packet)
        assertEquals(0xFE, b.get().toInt() and 0xFF)
        assertEquals(0, b.short.toInt())
    }

    @Test
    fun `gps packet matches the documented layout`() {
        val packet = Protocol.encodeGps(
            timestampMs = 1_700_000_000_000L,
            latitude = 12.971598,
            longitude = 77.594566,
            altitudeM = 920.5f,
            accuracyM = 3.25f
        )
        assertEquals(Protocol.GPS_PACKET_SIZE, packet.size)

        val b = reader(packet)
        assertEquals("header", 0xFD, b.get().toInt() and 0xFF)
        assertEquals(1_700_000_000_000L, b.long)
        assertEquals(12.971598, b.double, 1e-12)
        assertEquals(77.594566, b.double, 1e-12)
        assertEquals(920.5f, b.float, 0f)
        assertEquals(3.25f, b.float, 0f)
    }

    @Test
    fun `handshake ack is parsed`() {
        val bytes = ByteBuffer.allocate(Protocol.HANDSHAKE_ACK_SIZE)
            .order(ByteOrder.BIG_ENDIAN)
            .put(0xAA.toByte())
            .putInt(1)
            .put(0.toByte())
            .array()

        val ack = Protocol.readHandshakeAck(DataInputStream(ByteArrayInputStream(bytes)))
        assertEquals(1, ack?.protocolVersion)
        assertEquals(true, ack?.ok)
    }

    @Test
    fun `handshake ack reports a non-zero status as not ok`() {
        val bytes = ByteBuffer.allocate(Protocol.HANDSHAKE_ACK_SIZE)
            .order(ByteOrder.BIG_ENDIAN)
            .put(0xAA.toByte())
            .putInt(1)
            .put(1.toByte())
            .array()

        val ack = Protocol.readHandshakeAck(DataInputStream(ByteArrayInputStream(bytes)))
        assertEquals(false, ack?.ok)
    }

    /**
     * A receiver that starts reading without greeting us is still usable, so a
     * wrong or truncated greeting must come back as null rather than throwing.
     */
    @Test
    fun `unexpected greeting yields null rather than throwing`() {
        val notAnAck = byteArrayOf(0x01, 0x02, 0x03)
        assertNull(Protocol.readHandshakeAck(DataInputStream(ByteArrayInputStream(notAnAck))))
    }

    @Test
    fun `truncated greeting yields null rather than throwing`() {
        val truncated = byteArrayOf(0xAA.toByte(), 0x00, 0x00)
        assertNull(Protocol.readHandshakeAck(DataInputStream(ByteArrayInputStream(truncated))))
    }

    // ------------------------------------------------------------ protocol v2
    // Byte layouts rtvio/src/rtvio/stream/protocol.py reads (read_status,
    // read_frame for PREVIEW) and writes (encode_command).

    @Test
    fun `status packet is u8 header, u16 length, utf8 json`() {
        val json = """{"state":"armed","model":"Pixel ✓"}"""
        val packet = Protocol.encodeStatus(json)
        val body = json.toByteArray(Charsets.UTF_8)
        assertEquals(3 + body.size, packet.size)
        val b = reader(packet)
        assertEquals(0xFB, b.get().toInt() and 0xFF)
        assertEquals(body.size, b.short.toInt() and 0xFFFF)
        val got = ByteArray(body.size).also { b.get(it) }
        assertTrue(body.contentEquals(got))
    }

    @Test
    fun `preview packet is a frame packet with its own header`() {
        val jpeg = byteArrayOf(1, 2, 3, 4, 5)
        val packet = Protocol.encodeFrame(42L, 960, 1280, jpeg, header = Protocol.HEADER_PREVIEW)
        assertEquals(Protocol.FRAME_OVERHEAD + jpeg.size, packet.size)
        val b = reader(packet)
        assertEquals(0xFA, b.get().toInt() and 0xFF)
        assertEquals(42L, b.long)
        assertEquals(960, b.int)
        assertEquals(1280, b.int)
        assertEquals(jpeg.size, b.int)
    }

    @Test
    fun `command packet from the desktop is read back`() {
        val json = """{"cmd":"start","session":"20260914-190000","fps":30}"""
        val body = json.toByteArray(Charsets.UTF_8)
        val bytes = ByteBuffer.allocate(3 + body.size).order(ByteOrder.BIG_ENDIAN)
            .put(0xC0.toByte()).putShort(body.size.toShort()).put(body).array()
        val input = DataInputStream(ByteArrayInputStream(bytes + bytes))
        assertEquals(json, Protocol.readCommand(input))
        assertEquals("a second packet on the same stream", json, Protocol.readCommand(input))
    }

    @Test(expected = java.io.IOException::class)
    fun `a non-command byte from the desktop is a stream error`() {
        Protocol.readCommand(DataInputStream(ByteArrayInputStream(byteArrayOf(0x42, 0, 0))))
    }

    @Test
    fun `only a v2 greeting turns on remote control`() {
        val v1 = com.rtvio.mapper.net.ConnectionInfo(com.rtvio.mapper.net.ConnectionState.CONNECTED, serverVersion = 1)
        val v2 = com.rtvio.mapper.net.ConnectionInfo(com.rtvio.mapper.net.ConnectionState.CONNECTED, serverVersion = 2)
        val v2down = v2.copy(state = com.rtvio.mapper.net.ConnectionState.ERROR)
        assertEquals(false, v1.remoteControl)
        assertEquals(true, v2.remoteControl)
        assertEquals(false, v2down.remoteControl)
    }

    /** The count field is a signed 16-bit int; overflowing it must be loud. */
    @Test(expected = IllegalArgumentException::class)
    fun `oversized imu batch is rejected`() {
        val one = ImuSample(0, 0f, 0f, 0f, 0f, 0f, 0f)
        Protocol.encodeImuBatch(List(1) { one }, count = 40_000)
    }
}
