package com.rtvio.mapper.net

import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile

/**
 * A disk-backed FIFO of encoded packets: the reason a recording never drops a
 * frame.
 *
 * Live streaming bounds its video queue and evicts the oldest frame when WiFi
 * falls behind, because for a live view a fresh frame beats a stale one. A
 * recording for reconstruction wants the opposite - every frame, late is fine -
 * and the desktop measured what the live policy costs: a real capture that ran
 * at 27 fps on the phone delivered 7 fps to the desktop. So while recording,
 * every frame packet is appended here instead, and the socket writer drains
 * it in order. When the link is fast the file stays near-empty (each record
 * is read back moments after it is written, straight from the page cache);
 * when it is slow the backlog sits on flash, and after STOP the phone keeps
 * uploading until it is empty.
 *
 * Records are `i32 length` + packet bytes. The file is truncated back to zero
 * whenever the reader catches up, so it never grows past the largest backlog.
 *
 * Thread-safety: [append] (encoder thread) and [poll] (socket writer) may run
 * concurrently; both take the same monitor. A record is one write() call, so
 * the reader never sees half of one.
 */
class FrameSpool(private val file: File, private val minFreeBytes: Long = DEFAULT_MIN_FREE_BYTES) {

    companion object {
        /** Stop spooling (and start counting drops) below this much free storage. */
        const val DEFAULT_MIN_FREE_BYTES = 300L * 1024 * 1024
    }

    private val lock = Object()
    private val out: FileOutputStream
    private val reader: RandomAccessFile
    private var writePos = 0L
    private var readPos = 0L

    /** Records waiting to be sent. */
    @Volatile var pending = 0
        private set
    /** Bytes waiting to be sent. */
    val pendingBytes: Long get() = synchronized(lock) { writePos - readPos }
    /** Records ever appended / taken, for the session's frame accounting. */
    @Volatile var appended = 0L
        private set
    @Volatile var taken = 0L
        private set
    /** Records refused because storage was nearly full. */
    @Volatile var refused = 0L
        private set

    init {
        file.parentFile?.mkdirs()
        out = FileOutputStream(file, false)
        reader = RandomAccessFile(file, "r")
    }

    val freeBytes: Long get() = file.parentFile?.usableSpace ?: 0L

    /** @return false if the record was refused because storage is nearly full. */
    fun append(packet: ByteArray): Boolean {
        synchronized(lock) {
            // usableSpace is a statfs() call; checking it once per 32 records
            // is plenty at 30 fps and keeps it off the per-frame path.
            if (appended % 32 == 0L && freeBytes < minFreeBytes) {
                refused++
                return false
            }
            val rec = ByteArray(4 + packet.size)
            rec[0] = (packet.size ushr 24).toByte()
            rec[1] = (packet.size ushr 16).toByte()
            rec[2] = (packet.size ushr 8).toByte()
            rec[3] = packet.size.toByte()
            System.arraycopy(packet, 0, rec, 4, packet.size)
            out.write(rec)
            writePos += rec.size
            pending++
            appended++
            return true
        }
    }

    /** Next record in FIFO order, or null if the spool is empty. */
    fun poll(): ByteArray? {
        synchronized(lock) {
            if (readPos >= writePos) return null
            reader.seek(readPos)
            val len = reader.readInt()
            val buf = ByteArray(len)
            reader.readFully(buf)
            readPos += 4 + len
            pending--
            taken++
            if (readPos == writePos) {
                // Caught up: reclaim the space. out was opened without append
                // semantics, so reposition its channel explicitly as well.
                out.channel.truncate(0)
                out.channel.position(0)
                writePos = 0
                readPos = 0
            }
            return buf
        }
    }

    fun close(deleteFile: Boolean = true) {
        synchronized(lock) {
            try { out.close() } catch (_: Exception) {}
            try { reader.close() } catch (_: Exception) {}
            if (deleteFile) file.delete()
        }
    }
}
