package com.rtvio.mapper

import android.app.Application
import androidx.preference.PreferenceManager

class RtvioApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // Writes the defaults declared in root_preferences.xml on first launch,
        // so SettingsManager reads real values rather than falling back to its
        // own hardcoded defaults on a fresh install. readAgain = false makes
        // this a no-op on every launch after the first.
        PreferenceManager.setDefaultValues(this, R.xml.root_preferences, false)
    }
}
