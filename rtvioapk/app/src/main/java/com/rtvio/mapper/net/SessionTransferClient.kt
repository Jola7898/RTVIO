package com.rtvio.mapper.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.File
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Sends a [com.rtvio.mapper.capture.LocalSessionRecorder] session directory
 * to a desktop receiver over one dedicated TCP connection - the "Transfer"
 * side of recording fully offline and reconnecting later. Never mixed with
 * live streaming; see [Protocol.HEADER_SESSION_BEGIN].
 *
 * `mock_receiver.py --sessions-dir DIR` is the reference counterpart: it
 * writes the files back out under the same relative paths, so the result is
 * immediately usable with `rtvio.vggt_reconstruct --from-recording`.
 */
object SessionTransferClient {

    private const val CONNECT_TIMEOUT_MS = 5_000
    private const val HANDSHAKE_TIMEOUT_MS = 1_500
    private const val CHUNK_BYTES = 64 * 1024

    data class Progress(val sentBytes: Long, val totalBytes: Long, val currentFile: String)

    suspend fun transfer(
        sessionDir: File,
        host: String,
        port: Int,
        onProgress: (Progress) -> Unit = {}
    ): Result<Unit> = withContext(Dispatchers.IO) {
        try {
            val files = sessionDir.walkTopDown().filter { it.isFile }.sortedBy { it.path }.toList()
            if (files.isEmpty()) return@withContext Result.failure(IOException("nothing to send in ${sessionDir.name}"))
            val totalBytes = files.sumOf { it.length() }

            Socket().use { sock ->
                sock.tcpNoDelay = true
                sock.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)

                // The handshake is optional and ignored either way: a plain
                // v1 receiver that has no idea what a session transfer is
                // would otherwise be indistinguishable from one hanging.
                sock.soTimeout = HANDSHAKE_TIMEOUT_MS
                try {
                    Protocol.readHandshakeAck(DataInputStream(sock.getInputStream()))
                } catch (e: IOException) {
                    // No greeting within the timeout is fine; proceed anyway.
                }
                sock.soTimeout = 0

                val out = BufferedOutputStream(sock.getOutputStream(), 256 * 1024)
                out.write(Protocol.encodeSessionBegin(sessionDir.name, files.size, totalBytes))

                var sent = 0L
                val buf = ByteArray(CHUNK_BYTES)
                for (f in files) {
                    val rel = f.relativeTo(sessionDir).path.replace(File.separatorChar, '/')
                    out.write(Protocol.encodeSessionFileHeader(rel, f.length()))
                    f.inputStream().use { input ->
                        while (true) {
                            val n = input.read(buf)
                            if (n <= 0) break
                            out.write(buf, 0, n)
                            sent += n
                            onProgress(Progress(sent, totalBytes, rel))
                        }
                    }
                }
                out.write(Protocol.encodeSessionEnd())
                out.flush()
            }
            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
