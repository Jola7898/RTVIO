package com.rtvio.mapper.ui

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.rtvio.mapper.R
import com.rtvio.mapper.data.DeviceSpecsCollector
import com.rtvio.mapper.data.SpecItem
import com.rtvio.mapper.data.SpecSection
import com.rtvio.mapper.databinding.ActivitySpecsBinding
import com.rtvio.mapper.databinding.ItemSpecRowBinding
import com.rtvio.mapper.databinding.ItemSpecSectionBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Read-only listing of everything [DeviceSpecsCollector] could determine.
 *
 * Collection is done off the main thread: reading /proc, every camera's
 * characteristics and the sensor list adds up to tens of milliseconds, which is
 * enough to drop frames on the transition animation.
 */
class PhoneSpecsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySpecsBinding
    private lateinit var collector: DeviceSpecsCollector

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySpecsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        collector = DeviceSpecsCollector(this)
        binding.toolbar.setNavigationOnClickListener { finish() }
        binding.toolbar.inflateMenu(R.menu.specs_menu)
        binding.toolbar.setOnMenuItemClickListener { item ->
            if (item.itemId == R.id.menu_share) {
                shareSpecs(); true
            } else false
        }

        binding.specsList.layoutManager = LinearLayoutManager(this)

        lifecycleScope.launch {
            val sections = withContext(Dispatchers.IO) { collector.collectAll() }
            binding.specsList.adapter = SpecsAdapter(flatten(sections))
        }
    }

    private fun shareSpecs() {
        lifecycleScope.launch {
            val text = withContext(Dispatchers.IO) { collector.asPlainText() }
            startActivity(
                Intent.createChooser(
                    Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_SUBJECT, getString(R.string.title_specs))
                        putExtra(Intent.EXTRA_TEXT, text)
                    },
                    getString(R.string.menu_share)
                )
            )
        }
    }

    /** Section headers and rows share one list so the RecyclerView stays flat. */
    private fun flatten(sections: List<SpecSection>): List<Row> = buildList {
        sections.forEach { section ->
            add(Row.Header(section.title))
            section.items.forEach { add(Row.Value(it)) }
        }
    }

    private sealed class Row {
        data class Header(val title: String) : Row()
        data class Value(val item: SpecItem) : Row()
    }

    private class SpecsAdapter(private val rows: List<Row>) :
        RecyclerView.Adapter<RecyclerView.ViewHolder>() {

        companion object {
            const val TYPE_HEADER = 0
            const val TYPE_VALUE = 1
        }

        class HeaderHolder(val b: ItemSpecSectionBinding) : RecyclerView.ViewHolder(b.root)
        class ValueHolder(val b: ItemSpecRowBinding) : RecyclerView.ViewHolder(b.root)

        override fun getItemCount() = rows.size

        override fun getItemViewType(position: Int) =
            if (rows[position] is Row.Header) TYPE_HEADER else TYPE_VALUE

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
            val inflater = LayoutInflater.from(parent.context)
            return if (viewType == TYPE_HEADER) {
                HeaderHolder(ItemSpecSectionBinding.inflate(inflater, parent, false))
            } else {
                ValueHolder(ItemSpecRowBinding.inflate(inflater, parent, false))
            }
        }

        override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
            when (val row = rows[position]) {
                // The header layout is a bare TextView, so its binding root is
                // the TextView itself - no child lookup needed.
                is Row.Header -> (holder as HeaderHolder).b.root.text = row.title
                is Row.Value -> (holder as ValueHolder).b.apply {
                    specLabel.text = row.item.label
                    specValue.text = row.item.value
                }
            }
        }
    }
}
