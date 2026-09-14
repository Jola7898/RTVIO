package com.rtvio.mapper

import com.rtvio.mapper.net.FrameSpool
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class FrameSpoolTest {

    private fun spool(minFree: Long = 0L): Pair<FrameSpool, File> {
        val dir = Files.createTempDirectory("spool").toFile()
        val f = File(dir, "s.bin")
        return FrameSpool(f, minFreeBytes = minFree) to f
    }

    @Test
    fun `records come back in order and intact`() {
        val (s, _) = spool()
        val packets = (1..50).map { i -> ByteArray(1000 + i) { (it * i).toByte() } }
        packets.forEach { assertTrue(s.append(it)) }
        assertEquals(50, s.pending)
        packets.forEach { assertArrayEquals(it, s.poll()) }
        assertNull(s.poll())
        assertEquals(0, s.pending)
        assertEquals(50L, s.taken)
        s.close()
    }

    @Test
    fun `file is reclaimed once the reader catches up`() {
        val (s, f) = spool()
        repeat(20) { s.append(ByteArray(10_000)) }
        assertTrue(f.length() >= 20 * 10_004L)
        repeat(20) { s.poll() }
        assertEquals("truncated after draining", 0L, f.length())
        // ...and keeps working after the reset.
        s.append(byteArrayOf(1, 2, 3))
        assertArrayEquals(byteArrayOf(1, 2, 3), s.poll())
        s.close()
    }

    @Test
    fun `interleaved append and poll keep FIFO order`() {
        val (s, _) = spool()
        var next = 0
        var expect = 0
        repeat(200) { round ->
            repeat(round % 3 + 1) { s.append(byteArrayOf(next.toByte(), (next shr 8).toByte())); next++ }
            val r = s.poll()!!
            assertEquals(expect and 0xFF, r[0].toInt() and 0xFF)
            assertEquals((expect shr 8) and 0xFF, r[1].toInt() and 0xFF)
            expect++
        }
        while (true) {
            val r = s.poll() ?: break
            assertEquals(expect and 0xFF, r[0].toInt() and 0xFF)
            expect++
        }
        assertEquals(next, expect)
        s.close()
    }

    @Test
    fun `refuses records when storage is nearly full`() {
        val (s, _) = spool(minFree = Long.MAX_VALUE)
        assertFalse(s.append(ByteArray(10)))
        assertEquals(1L, s.refused)
        assertEquals(0, s.pending)
        s.close()
    }
}
