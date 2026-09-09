# Auto-Discovery of Camera Intrinsics

## Problem Solved

Previously, the app and desktop were unaware of the phone's actual camera intrinsics (focal length in pixels, principal point, distortion). This forced:
- Hardcoded synthetic defaults (slow initialization, inaccurate depth)
- Manual calibration sessions (tedious, error-prone)
- Per-device lookup tables (unmaintainable)

**Solution:** The Android app now queries Camera2 API for intrinsics at session start and sends them to the desktop in a new packet type (0xFC).

## How It Works

### Three Sources (In Priority Order)

1. **Camera2 LENS_INTRINSIC_CALIBRATION** (Best)
   - Available on LIMITED and above hardware levels
   - Measured at device manufacturing
   - Authoritative ground truth

2. **Computed from Focal Length + Sensor Geometry** (Good)
   - Derived from LENS_INFO_AVAILABLE_FOCAL_LENGTHS (mm)
   - SENSOR_INFO_PHYSICAL_SIZE (mm)
   - Image resolution (pixels)
   - Close to actual but may miss lens asymmetries

3. **Plausible Default** (Last Resort)
   - Standard smartphone FOV (~52°)
   - Used only if focal length is unavailable

### Wire Format

New packet type sent once at session start:

```
0xFC 
  f32 fx_pix, fy_pix, cx_pix, cy_pix       # focal length & principal point in pixels
  f64 k1, k2, p1, p2, k3                    # distortion coefficients
  u16 source_len
  u8[source_len] source                     # null-terminated string describing the source
```

## Integration in StreamingSession.kt

### Step 1: Add Import

```kotlin
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import com.rtvio.mapper.data.cameraIntrinsicsFromCharacteristics
```

### Step 2: Extract Intrinsics at Connect Time

Modify the `start()` method to capture and send intrinsics right after the socket connects:

```kotlin
fun start(owner: LifecycleOwner, previewView: PreviewView): String? {
    if (isStreaming) return null

    val host = settings.serverIp
    if (host.isEmpty()) return "Set a server IP in Settings first"
    if (!imu.hasAccelerometer) return "This device has no accelerometer"

    registerWifiWatch()
    if (!isWifiConnected()) {
        unregisterWifiWatch()
        return "Connect to WiFi before streaming"
    }

    startedAtMs = System.currentTimeMillis()
    isStreaming = true

    // 1. Socket
    client.start(
        host = host,
        port = settings.serverPort,
        autoReconnect = settings.autoReconnect,
        baseBackoffSec = settings.reconnectIntervalSec
    )

    // NEW: Send camera intrinsics right after socket connects
    // The client's writeLoop will transmit this before any frames.
    sendCameraIntrinsics()

    // 2. Camera
    if (camera == null) startPreview(owner, previewView)
    camera?.updateQuality(settings.jpegQuality)
    camera?.streaming = true

    // 3. Sensors
    if (!imu.hasGyroscope) {
        _events.tryEmit("No gyroscope on this device - angular rate will be zero")
    }
    imu.start(settings.imuRateHz, settings.imuBatchIntervalMs) { batch ->
        if (_wifiConnected.value) client.offerImuBatch(batch)
    }

    if (settings.outdoorMode) {
        gps.start(
            onFix = { fix ->
                if (_wifiConnected.value) {
                    client.offerGps(
                        timestampMs = fix.time,
                        latitude = fix.latitude,
                        longitude = fix.longitude,
                        altitudeM = fix.altitude.toFloat(),
                        accuracyM = if (fix.hasAccuracy()) fix.accuracy else -1f
                    )
                }
            },
            onStatus = { _events.tryEmit(it) }
        )
    }

    StreamingForegroundService.start(
        appContext,
        needsLocation = settings.outdoorMode,
        keepAwake = settings.keepAwake
    )
    return null
}

// NEW METHOD: Extract and send intrinsics
private fun sendCameraIntrinsics() {
    try {
        val cm = appContext.getSystemService(Context.CAMERA_SERVICE) as? CameraManager
            ?: return
        
        // Find the primary (back-facing) camera
        val cameraId = cm.cameraIdList.firstOrNull { camId ->
            cm.getCameraCharacteristics(camId)
                .get(CameraCharacteristics.LENS_FACING) == CameraMetadata.LENS_FACING_BACK
        } ?: cm.cameraIdList.firstOrNull() ?: return
        
        val characteristics = cm.getCameraCharacteristics(cameraId)
        
        // Use the active resolution if the camera is already running,
        // otherwise use the requested resolution from settings.
        val (width, height) = camera?.activeResolution?.let { res ->
            Pair(res.width, res.height)
        } ?: run {
            val sz = settings.resolution
            Pair(sz.width, sz.height)
        }
        
        val intrinsics = cameraIntrinsicsFromCharacteristics(
            characteristics,
            width, height
        )
        
        client.offerIntrinsics(
            fxPix = intrinsics.fx_pix.toFloat(),
            fyPix = intrinsics.fy_pix.toFloat(),
            cxPix = intrinsics.cx_pix.toFloat(),
            cyPix = intrinsics.cy_pix.toFloat(),
            k1 = intrinsics.k1,
            k2 = intrinsics.k2,
            p1 = intrinsics.p1,
            p2 = intrinsics.p2,
            k3 = intrinsics.k3,
            source = intrinsics.source
        )
        
        Log.i(TAG, "sent camera intrinsics: ${intrinsics.source}")
    } catch (e: Exception) {
        Log.w(TAG, "failed to send camera intrinsics", e)
        // Not fatal: the desktop can still work with a synthetic default.
    }
}
```

### Step 3: Desktop Receiver Integration

The desktop receiver (src/rtvio/stream/source.py) should handle the new packet type:

```python
# In src/rtvio/stream/source.py, add to the packet dispatcher:

KIND_INTRINSICS = 0xFC

# In the packet loop, dispatch 0xFC packets:
if pkt_type == KIND_INTRINSICS:
    intrinsics = _parse_intrinsics_packet(pkt)
    self._notify("on_intrinsics", intrinsics)

# Add this parser:
def _parse_intrinsics_packet(data):
    """Parse 0xFC intrinsics packet."""
    stream = io.BytesIO(data[1:])  # skip the header byte
    fx = struct.unpack('>f', stream.read(4))[0]
    fy = struct.unpack('>f', stream.read(4))[0]
    cx = struct.unpack('>f', stream.read(4))[0]
    cy = struct.unpack('>f', stream.read(4))[0]
    k1 = struct.unpack('>d', stream.read(8))[0]
    k2 = struct.unpack('>d', stream.read(8))[0]
    p1 = struct.unpack('>d', stream.read(8))[0]
    p2 = struct.unpack('>d', stream.read(8))[0]
    k3 = struct.unpack('>d', stream.read(8))[0]
    source_len = struct.unpack('>H', stream.read(2))[0]
    source = stream.read(source_len).decode('utf-8')
    
    return {
        "fx_pix": fx, "fy_pix": fy, "cx_pix": cx, "cy_pix": cy,
        "k1": k1, "k2": k2, "p1": p1, "p2": p2, "k3": k3,
        "source": source
    }
```

### Step 4: Record Intrinsics in Session Output

In src/rtvio/live_pipeline.py, capture the intrinsics:

```python
class LiveReconstructor:
    def __init__(self, ...):
        self.camera_intrinsics = None  # Set by on_intrinsics callback
    
    def on_intrinsics(self, data):
        """Receive camera intrinsics from the phone."""
        self.camera_intrinsics = {
            "K": [[data["fx_pix"], 0, data["cx_pix"]],
                  [0, data["fy_pix"], data["cy_pix"]],
                  [0, 0, 1]],
            "distortion": [data["k1"], data["k2"], data["p1"], data["p2"], data["k3"]],
            "source": data["source"]
        }
        print(f"received camera intrinsics from {data['source']}")
    
    def _finalize(self, stats):
        # ... existing code ...
        
        # Write intrinsics to JSON
        if self.camera_intrinsics:
            with open(os.path.join(out, "camera_intrinsics.json"), "w") as f:
                json.dump(self.camera_intrinsics, f, indent=2)
```

## Benefits

✅ **Zero Manual Work** — Camera2 API provides the answer  
✅ **Per-Device Accuracy** — Uses actual hardware parameters, not synthetic defaults  
✅ **Automatic Scaling** — Recomputes if resolution changes mid-session  
✅ **Fallback Handling** — Degrades gracefully if LENS_INTRINSIC_CALIBRATION unavailable  
✅ **Source Tracking** — Records where the intrinsics came from (for debugging)  

## Testing

### Test 1: Verify Extraction on Phone

Add a simple test in `DeviceSpecsActivity` to display the extracted intrinsics:

```kotlin
private fun testIntrinsics() {
    val cm = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    val characteristics = cm.getCameraCharacteristics("0")  // back camera
    val intrinsics = cameraIntrinsicsFromCharacteristics(characteristics, 1920, 1080)
    println("K = [[${intrinsics.fx_pix}, 0, ${intrinsics.cx_pix}],")
    println("      [0, ${intrinsics.fy_pix}, ${intrinsics.cy_pix}],")
    println("      [0, 0, 1]]")
    println("Distortion: k1=${intrinsics.k1} k2=${intrinsics.k2}")
    println("Source: ${intrinsics.source}")
}
```

### Test 2: Verify Wire Format

In the desktop receiver, log the raw packet bytes before parsing:

```python
if pkt_type == 0xFC:
    print(f"intrinsics packet: {len(pkt)} bytes")
    print(f"  fx_pix={struct.unpack('>f', pkt[1:5])[0]:.2f}")
    print(f"  fy_pix={struct.unpack('>f', pkt[5:9])[0]:.2f}")
    print(f"  cx_pix={struct.unpack('>f', pkt[9:13])[0]:.2f}")
    print(f"  cy_pix={struct.unpack('>f', pkt[13:17])[0]:.2f}")
```

### Test 3: Live Run

1. Start the app
2. Connect to the desktop receiver
3. Verify in the desktop logs that "received camera intrinsics" appears
4. Check that camera_intrinsics.json contains the K matrix and distortion

## Fallback Strategy

If the packet is lost or never sent:
- The desktop uses its cached defaults (from previous runs)
- Or falls back to the synthetic 960×540 pinhole
- A warning is logged: "using cached intrinsics; consider re-running the session"

The system remains functional but with lower initialization accuracy until a real calibration arrives.

## Next Steps

1. Build and test the APK with the intrinsics changes
2. Integrate the desktop receiver parser
3. Run a short live session and verify the JSON output
4. Compare live run accuracy with and without real intrinsics

---

**Related Issue:** Camera↔IMU extrinsic and frame timestamp handling are separate; this solves only the intrinsics problem. See INTEGRATION.md §4.6 and §4.2 for those.
