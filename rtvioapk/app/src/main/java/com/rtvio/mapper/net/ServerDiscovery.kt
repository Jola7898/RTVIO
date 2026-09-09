package com.rtvio.mapper.net

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.util.Log
import java.net.Inet4Address

/**
 * Finds RTVIO receivers advertising `_rtvio._tcp` on the local network.
 *
 * Uses the platform's NsdManager rather than a bundled mDNS stack. The spec's
 * suggested dependencies (bouncycastle, java-chassis) do not implement mDNS;
 * NsdManager does, ships with the OS, and costs the APK nothing.
 *
 * Discovery is best-effort by nature. Many networks block multicast, and some
 * vendor WiFi stacks silently drop it in power-save. The manual IP field in
 * settings is therefore the supported path, and this is the convenience on top.
 */
class ServerDiscovery(context: Context) {

    companion object {
        private const val TAG = "ServerDiscovery"
        const val SERVICE_TYPE = "_rtvio._tcp."
    }

    /** A receiver we found, ready to be written into settings. */
    data class Server(val name: String, val host: String, val port: Int) {
        override fun toString() = "$name  ($host:$port)"
    }

    private val nsdManager =
        context.applicationContext.getSystemService(Context.NSD_SERVICE) as? NsdManager

    private var listener: NsdManager.DiscoveryListener? = null

    /** Names already resolved or in flight, so a re-announce is not listed twice. */
    private val seen = mutableSetOf<String>()

    /**
     * Starts browsing. [onFound] fires on an arbitrary binder thread once per
     * distinct server; [onError] fires if discovery cannot start at all.
     */
    fun start(onFound: (Server) -> Unit, onError: (String) -> Unit) {
        val nsd = nsdManager ?: run {
            onError("Network service discovery is unavailable on this device")
            return
        }
        stop()
        seen.clear()

        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.i(TAG, "browsing $serviceType")
            }

            override fun onServiceFound(info: NsdServiceInfo) {
                if (!seen.add(info.serviceName)) return
                resolve(nsd, info, onFound)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                seen.remove(info.serviceName)
            }

            override fun onDiscoveryStopped(serviceType: String) {
                Log.i(TAG, "discovery stopped")
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                onError("Could not start discovery (error $errorCode)")
                stop()
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "stop discovery failed: $errorCode")
            }
        }

        listener = l
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
        } catch (e: IllegalArgumentException) {
            listener = null
            onError(e.message ?: "Discovery rejected")
        }
    }

    /**
     * Resolution is serialised behind a single in-flight request on older
     * platforms; a second concurrent resolve returns FAILURE_ALREADY_ACTIVE.
     * Retrying once after a short delay covers the common two-servers case
     * without building a full request queue.
     */
    @Suppress("DEPRECATION") // registerServiceInfoCallback is API 34+; minSdk is 24.
    private fun resolve(nsd: NsdManager, info: NsdServiceInfo, onFound: (Server) -> Unit) {
        nsd.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(failed: NsdServiceInfo, errorCode: Int) {
                if (errorCode == NsdManager.FAILURE_ALREADY_ACTIVE) {
                    Thread.sleep(250)
                    seen.remove(failed.serviceName)
                } else {
                    Log.w(TAG, "resolve failed for ${failed.serviceName}: $errorCode")
                }
            }

            override fun onServiceResolved(resolved: NsdServiceInfo) {
                val address = hostAddressOf(resolved) ?: return
                onFound(Server(resolved.serviceName, address, resolved.port))
            }
        })
    }

    /**
     * Prefers an IPv4 literal: the receiver binds a plain IPv4 socket, and an
     * IPv6 link-local address would need a scope suffix the settings field
     * cannot round-trip.
     */
    private fun hostAddressOf(info: NsdServiceInfo): String? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            info.hostAddresses.firstOrNull { it is Inet4Address }?.let { return it.hostAddress }
            return info.hostAddresses.firstOrNull()?.hostAddress
        }
        @Suppress("DEPRECATION")
        return info.host?.hostAddress
    }

    fun stop() {
        val l = listener ?: return
        listener = null
        try {
            nsdManager?.stopServiceDiscovery(l)
        } catch (e: IllegalArgumentException) {
            // Already stopped, or never successfully started.
        }
    }
}
