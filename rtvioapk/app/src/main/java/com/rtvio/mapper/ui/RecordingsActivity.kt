package com.rtvio.mapper.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import com.rtvio.mapper.R
import com.rtvio.mapper.data.LocalSessions
import com.rtvio.mapper.data.SettingsManager
import com.rtvio.mapper.databinding.ActivityRecordingsBinding
import com.rtvio.mapper.databinding.ItemRecordingRowBinding
import com.rtvio.mapper.net.ReceiverProbe
import com.rtvio.mapper.net.SessionTransferClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The other half of "record with no PC in reach, connect later": sessions
 * [com.rtvio.mapper.capture.LocalSessionRecorder] saved fully offline, each
 * transferable to the desktop once its receiver is reachable, or deletable
 * once it no longer needs to be.
 */
class RecordingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivityRecordingsBinding
    private lateinit var settings: SettingsManager

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityRecordingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        settings = SettingsManager(this)

        binding.toolbar.setNavigationOnClickListener { finish() }
        binding.list.layoutManager = LinearLayoutManager(this)
        reload()
    }

    override fun onResume() {
        super.onResume()
        reload()
    }

    private fun reload() {
        val entries = LocalSessions.list(this)
        binding.emptyState.visibility = if (entries.isEmpty()) View.VISIBLE else View.GONE
        binding.list.visibility = if (entries.isEmpty()) View.GONE else View.VISIBLE
        binding.list.adapter = RecordingsAdapter(entries) { entry -> showActions(entry) }
    }

    private fun showActions(entry: LocalSessions.Entry) {
        MaterialAlertDialogBuilder(this)
            .setTitle(entry.id)
            .setItems(
                arrayOf(getString(R.string.recordings_transfer), getString(R.string.recordings_delete))
            ) { _, which -> if (which == 0) transfer(entry) else confirmDelete(entry) }
            .show()
    }

    private fun confirmDelete(entry: LocalSessions.Entry) {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.recordings_delete_title)
            .setMessage(getString(R.string.recordings_delete_message, entry.frameCount))
            .setPositiveButton(R.string.recordings_delete) { _, _ -> LocalSessions.delete(entry); reload() }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun transfer(entry: LocalSessions.Entry) {
        val host = settings.serverIp
        val port = settings.serverPort
        val dialog = MaterialAlertDialogBuilder(this)
            .setMessage(getString(R.string.recordings_transferring, entry.id))
            .setCancelable(false)
            .show()
        lifecycleScope.launch {
            val reachable = withContext(Dispatchers.IO) { ReceiverProbe.isReachable(host, port, 2000) }
            if (!reachable) {
                dialog.dismiss()
                Snackbar.make(
                    binding.root,
                    getString(R.string.recordings_no_receiver, host.ifEmpty { getString(R.string.no_server_set) }, port),
                    Snackbar.LENGTH_LONG
                ).show()
                return@launch
            }
            val result = SessionTransferClient.transfer(entry.dir, host, port)
            dialog.dismiss()
            result.onSuccess {
                Snackbar.make(
                    binding.root, getString(R.string.recordings_transfer_done, entry.id, host), Snackbar.LENGTH_LONG
                ).show()
                offerDeleteAfterTransfer(entry)
            }.onFailure { e ->
                Snackbar.make(
                    binding.root,
                    getString(R.string.recordings_transfer_failed, e.message ?: e.javaClass.simpleName),
                    Snackbar.LENGTH_LONG
                ).show()
            }
        }
    }

    private fun offerDeleteAfterTransfer(entry: LocalSessions.Entry) {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.recordings_delete_after_transfer_title)
            .setMessage(getString(R.string.recordings_delete_after_transfer_message, entry.id))
            .setPositiveButton(R.string.recordings_delete) { _, _ -> LocalSessions.delete(entry); reload() }
            .setNegativeButton(R.string.keep, null)
            .show()
    }
}

private class RecordingsAdapter(
    private val entries: List<LocalSessions.Entry>,
    private val onClick: (LocalSessions.Entry) -> Unit
) : RecyclerView.Adapter<RecordingsAdapter.Holder>() {

    private val dateFormat = SimpleDateFormat("d MMM yyyy, HH:mm", Locale.getDefault())

    class Holder(val b: ItemRecordingRowBinding) : RecyclerView.ViewHolder(b.root)

    override fun getItemCount() = entries.size

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
        Holder(ItemRecordingRowBinding.inflate(LayoutInflater.from(parent.context), parent, false))

    override fun onBindViewHolder(holder: Holder, position: Int) {
        val e = entries[position]
        holder.b.recTitle.text = e.id
        holder.b.recSubtitle.text = "%s  ·  %,d frames  ·  %.1f MB%s".format(
            dateFormat.format(Date(e.createdAtMs)), e.frameCount, e.sizeBytes / 1e6,
            if (e.hasGps) "  ·  GPS" else ""
        )
        holder.b.root.setOnClickListener { onClick(e) }
    }
}
