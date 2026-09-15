package com.rtvio.mapper.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.InetSocketAddress
import java.net.Socket

/**
 * "Is anything listening at Settings -> Server IP right now?" - the check
 * that lets the main screen offer STREAM/RECORD LOCALLY/TRANSFER instead of
 * only finding out a receiver is missing after a failed CONNECT.
 *
 * A bare TCP connect-then-close is enough: `mock_receiver.py` and
 * `rtvio.live_pipeline` both accept the connection, send their handshake, and
 * treat the immediate close exactly like any other client disconnecting
 * before sending a byte - already exercised in practice, since that is also
 * what happens on every reconnect backoff.
 */
object ReceiverProbe {

    suspend fun isReachable(host: String, port: Int, timeoutMs: Int = 1500): Boolean {
        if (host.isBlank()) return false
        return withContext(Dispatchers.IO) {
            try {
                Socket().use { it.connect(InetSocketAddress(host, port), timeoutMs) }
                true
            } catch (e: Exception) {
                false
            }
        }
    }
}
