package com.rtvio.mapper.data

import android.content.Context
import java.io.File

/**
 * Finds and manages the sessions [com.rtvio.mapper.capture.LocalSessionRecorder]
 * wrote to app-external storage - no permission needed on modern Android, and
 * visible over USB/MTP under Android/data/com.rtvio.mapper/files/recordings
 * as a manual fallback to the in-app Transfer flow.
 */
object LocalSessions {

    data class Entry(
        val id: String,
        val dir: File,
        val frameCount: Int,
        val sizeBytes: Long,
        val createdAtMs: Long,
        val hasGps: Boolean
    )

    fun rootDir(context: Context): File =
        File(context.getExternalFilesDir(null) ?: context.filesDir, "recordings")

    /** Newest first. A directory missing frame_timestamps.json is not a finished session. */
    fun list(context: Context): List<Entry> {
        val root = rootDir(context)
        val dirs = root.listFiles { f -> f.isDirectory } ?: return emptyList()
        return dirs.mapNotNull { d ->
            if (!File(d, "frame_timestamps.json").isFile) return@mapNotNull null
            val frameCount = File(d, "frames").listFiles { f -> f.extension == "jpg" }?.size ?: 0
            if (frameCount == 0) return@mapNotNull null
            val size = d.walkTopDown().filter { it.isFile }.sumOf { it.length() }
            val hasGps = File(d, "gps_data.json").let { it.isFile && it.length() > 2 }
            Entry(d.name, d, frameCount, size, d.lastModified(), hasGps)
        }.sortedByDescending { it.createdAtMs }
    }

    fun delete(entry: Entry): Boolean = entry.dir.deleteRecursively()
}
