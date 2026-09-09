# Keep the packet encoder's public surface readable in stack traces from the
# field; it is the one place a wire-format bug would show up as a crash.
-keepnames class com.rtvio.mapper.net.Protocol

# CameraX reflects on Camera2 interop extension points.
-keep class androidx.camera.camera2.** { *; }
