package com.rtvio.mapper.ui

import android.content.SharedPreferences
import android.os.Bundle
import android.text.InputType
import android.widget.ArrayAdapter
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AlertDialog
import androidx.lifecycle.lifecycleScope
import androidx.preference.EditTextPreference
import androidx.preference.Preference
import androidx.preference.PreferenceFragmentCompat
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.rtvio.mapper.R
import com.rtvio.mapper.data.SettingsManager
import com.rtvio.mapper.databinding.ActivitySettingsBinding
import com.rtvio.mapper.net.ServerDiscovery
import com.rtvio.mapper.net.StreamClient
import kotlinx.coroutines.launch

class SettingsActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.toolbar.setNavigationOnClickListener { finish() }

        if (savedInstanceState == null) {
            supportFragmentManager.beginTransaction()
                .replace(R.id.settingsContainer, SettingsFragment())
                .commit()
        }
    }
}

/**
 * The settings screen.
 *
 * Everything persists through androidx.preference into the default
 * SharedPreferences, which is exactly where [SettingsManager] reads from - so
 * there is no separate save step and no chance of the two drifting apart.
 */
class SettingsFragment : PreferenceFragmentCompat(),
    SharedPreferences.OnSharedPreferenceChangeListener {

    private lateinit var settings: SettingsManager
    private var discovery: ServerDiscovery? = null

    override fun onCreatePreferences(savedInstanceState: Bundle?, rootKey: String?) {
        setPreferencesFromResource(R.xml.root_preferences, rootKey)
        settings = SettingsManager(requireContext())

        // Numeric keyboards for the numeric fields. androidx.preference has no
        // XML attribute for this; the binding hook is the supported route.
        numericField("server_port")
        numericField("reconnect_interval")

        findPreference<EditTextPreference>("server_ip")?.apply {
            setOnBindEditTextListener { it.inputType = InputType.TYPE_CLASS_TEXT }
            summaryProvider = Preference.SummaryProvider<EditTextPreference> { pref ->
                pref.text?.takeIf { it.isNotBlank() }
                    ?: getString(R.string.pref_server_ip_summary)
            }
        }

        findPreference<Preference>("discover")?.setOnPreferenceClickListener {
            startDiscovery(); true
        }
        findPreference<Preference>("test_connection")?.setOnPreferenceClickListener {
            testConnection(); true
        }

        refreshBandwidthEstimate()
    }

    private fun numericField(key: String) {
        findPreference<EditTextPreference>(key)?.setOnBindEditTextListener {
            it.inputType = InputType.TYPE_CLASS_NUMBER
            it.setSelection(it.text.length)
        }
    }

    override fun onResume() {
        super.onResume()
        preferenceManager.sharedPreferences?.registerOnSharedPreferenceChangeListener(this)
    }

    override fun onPause() {
        super.onPause()
        preferenceManager.sharedPreferences?.unregisterOnSharedPreferenceChangeListener(this)
    }

    override fun onDestroyView() {
        discovery?.stop()
        discovery = null
        super.onDestroyView()
    }

    override fun onSharedPreferenceChanged(prefs: SharedPreferences?, key: String?) {
        // Resolution, frame rate and quality all move the bandwidth number.
        refreshBandwidthEstimate()
    }

    /**
     * Keeps the estimated uplink in front of the user while they change the
     * settings that drive it - the alternative is discovering at the far end of
     * a field survey that the link could never have carried the configuration.
     */
    private fun refreshBandwidthEstimate() {
        findPreference<Preference>("bandwidth_estimate")?.summary =
            getString(R.string.bandwidth_estimate, "%.1f".format(settings.estimateMbps()))
    }

    // ------------------------------------------------------------- discovery

    private fun startDiscovery() {
        val found = mutableListOf<ServerDiscovery.Server>()
        val adapter = ArrayAdapter<String>(
            requireContext(), android.R.layout.simple_list_item_1
        )

        val dialog: AlertDialog = MaterialAlertDialogBuilder(requireContext())
            // An AlertDialog shows either a message or a list, never both, so
            // the "searching" state lives in the title until a result lands.
            .setTitle(R.string.discovering)
            .setAdapter(adapter) { _, which -> found.getOrNull(which)?.let(::applyServer) }
            .setNegativeButton(R.string.cancel, null)
            .setOnDismissListener {
                discovery?.stop()
                discovery = null
            }
            .show()

        val d = ServerDiscovery(requireContext())
        discovery = d
        d.start(
            onFound = { server ->
                // NsdManager resolves on a binder thread.
                activity?.runOnUiThread {
                    if (found.none { it.host == server.host && it.port == server.port }) {
                        found += server
                        adapter.add(server.toString())
                        adapter.notifyDataSetChanged()
                        dialog.setTitle(R.string.discovery_title)
                    }
                }
            },
            onError = { message ->
                activity?.runOnUiThread {
                    dialog.dismiss()
                    toast(message)
                }
            }
        )

        // Multicast is unreliable enough that a silent empty dialog would be
        // misread as "no receiver running"; say so explicitly instead.
        view?.postDelayed({
            if (dialog.isShowing && found.isEmpty()) {
                dialog.dismiss()
                MaterialAlertDialogBuilder(requireContext())
                    .setMessage(R.string.discovery_none)
                    .setPositiveButton(R.string.ok, null)
                    .show()
            }
        }, 8000)
    }

    /** Writing through the preferences persists and refreshes the rows in one step. */
    private fun applyServer(server: ServerDiscovery.Server) {
        findPreference<EditTextPreference>("server_ip")?.text = server.host
        findPreference<EditTextPreference>("server_port")?.text = server.port.toString()
    }

    // ---------------------------------------------------------------- testing

    private fun testConnection() {
        val host = settings.serverIp
        if (host.isEmpty()) {
            toast(getString(R.string.no_server_set))
            return
        }
        val pref = findPreference<Preference>("test_connection") ?: return
        pref.summary = getString(R.string.testing)
        pref.isEnabled = false

        lifecycleScope.launch {
            val result = StreamClient.testConnection(host, settings.serverPort)
            pref.isEnabled = true
            pref.summary = result.fold(
                onSuccess = { it },
                onFailure = { "Failed: ${it.message ?: it.javaClass.simpleName}" }
            )
        }
    }

    private fun toast(message: String) =
        android.widget.Toast.makeText(requireContext(), message, android.widget.Toast.LENGTH_LONG)
            .show()
}
