"""
RTVIO wire protocol: bytes <-> typed packets.

Canonical definition is rtvioapk/.../net/Protocol.kt; this is the Python
side of the same contract, and the struct formats below are byte-for-byte
what rtvioapk/tools/mock_receiver.py already validates end-to-end against
the app. All fields are big-endian.

Kept deliberately free of sockets and of session state so the same parser
serves the live socket, the replay driver and the unit tests. The only
socket-aware function is `recv_exact`, and it is here because getting it
wrong is the single most common bug when writing a receiver:
**sock.recv(n) may return fewer than n bytes.** A 200 KB JPEG essentially
always arrives split across segments; reading a length field and calling
recv once works on localhost and fails over WiFi.
"""
import json
import struct
from typing import NamedTuple

HEADER_FRAME = 0xFF
HEADER_IMU = 0xFE
HEADER_GPS = 0xFD
HEADER_INTRINSICS = 0xFC
HEADER_STATUS = 0xFB           # phone -> desktop, JSON (protocol v2 only)
HEADER_PREVIEW = 0xFA          # phone -> desktop, same layout as FRAME; a
                               # low-rate viewfinder image sent while armed,
                               # never recorded (protocol v2 only)
HEADER_COMMAND = 0xC0          # desktop -> phone, JSON (protocol v2 only)
HEADER_HANDSHAKE_ACK = 0xAA
# phone -> desktop, one-shot bulk copy of a RECORD LOCALLY session (the app's
# Saved sessions -> Transfer action), its own dedicated connection, never
# mixed with live streaming. Canonical definition is Protocol.kt; wire format
# matches rtvioapk/tools/mock_receiver.py's receive_session_transfer exactly:
# SESSION_BEGIN (pstring id, i32 file_count, i64 total_bytes), then
# file_count x SESSION_FILE (pstring rel_path, i64 size, raw bytes), then
# SESSION_END.
HEADER_SESSION_BEGIN = 0xE0
HEADER_SESSION_FILE = 0xE1
HEADER_SESSION_END = 0xE2

# v1: the phone streams as soon as it connects and never reads another byte
# after the handshake. v2 (rtvio.studio): the phone connects "armed", streams
# only between a START and a STOP command, and reports its state in STATUS
# packets. The app decides which behaviour to use from the version in the
# handshake, so a v1 receiver (live_pipeline.py, mock_receiver.py) keeps
# working with the new app unchanged.
PROTOCOL_VERSION = 1
STUDIO_PROTOCOL_VERSION = 2
STATUS_OK = 0

HANDSHAKE_FMT = ">BIB"          # header, version, status
FRAME_HEADER_FMT = ">qiii"      # timestamp_ms, width, height, jpeg_size
IMU_COUNT_FMT = ">h"            # sample count
IMU_SAMPLE_FMT = ">qffffff"     # t_ns, ax, ay, az, gx, gy, gz
GPS_FMT = ">qddff"              # timestamp_ms, lat, lon, alt_m, accuracy_m

FRAME_HEADER_BYTES = struct.calcsize(FRAME_HEADER_FMT)
IMU_SAMPLE_BYTES = struct.calcsize(IMU_SAMPLE_FMT)
GPS_BYTES = struct.calcsize(GPS_FMT)

# Sanity ceilings so a desynchronised stream cannot make us allocate wildly.
MAX_JPEG_BYTES = 32 * 1024 * 1024
MAX_IMU_BATCH = 4096


class StreamClosed(Exception):
    """The peer hung up, or the stream desynchronised past recovery."""


class FramePacket(NamedTuple):
    """A JPEG still. `timestamp_ms` is WALL CLOCK (System.currentTimeMillis)."""
    timestamp_ms: int
    width: int
    height: int
    jpeg: bytes


class ImuSample(NamedTuple):
    """One accelerometer sample with the gyro already interpolated onto its
    timestamp by the app (see SensorDataCollector's class comment) - so the
    two triples are genuinely simultaneous and need no re-alignment here.

    `t_ns` is MONOTONIC SINCE BOOT (SensorEvent.timestamp), a different
    clock domain from every other packet. See clock.py.

    accel is m/s^2 and INCLUDES GRAVITY (TYPE_ACCELEROMETER, not
    TYPE_LINEAR_ACCELERATION), which is what so3.level_and_align_attitude
    requires. gyro is rad/s.
    """
    t_ns: int
    accel: tuple
    gyro: tuple


class GpsPacket(NamedTuple):
    """`timestamp_ms` is WALL CLOCK (Location.getTime). `altitude_m` is
    documented as metres above the WGS84 ellipsoid, but vendor
    implementations vary and some return orthometric height - a 20-100 m
    systematic Z offset that is very hard to spot downstream.

    `accuracy_m` is a metre-scale 1-sigma horizontal estimate, or -1 when
    unknown. live_pipeline.py's on_gps re-anchors the pose directly on each
    fix rather than fusing it, so this value isn't consumed as a filter
    covariance any more - kept on the wire (and in AltitudeSanity/reports)
    as a real per-fix quality signal.
    """
    timestamp_ms: int
    lat_deg: float
    lon_deg: float
    altitude_m: float
    accuracy_m: float

    @property
    def accuracy_known(self):
        return self.accuracy_m >= 0.0


class IntrinsicsPacket(NamedTuple):
    """Camera intrinsic matrix K and distortion coefficients from the phone.

    Sent once at session start. The phone has measured these from Camera2 API
    or computed from focal length and sensor size; either way, they are the
    ground truth for this device's camera at this resolution.

    K = [[fx_pix, 0, cx_pix], [0, fy_pix, cy_pix], [0, 0, 1]]
    distortion = [k1, k2, p1, p2, k3]
    """
    fx_pix: float
    fy_pix: float
    cx_pix: float
    cy_pix: float
    k1: float
    k2: float
    p1: float
    p2: float
    k3: float
    source: str  # Description of how these were obtained


def recv_exact(sock, count):
    """Read exactly `count` bytes from `sock`, or raise StreamClosed."""
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise StreamClosed(f"peer closed with {remaining} of {count} bytes outstanding")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_pstring(sock):
    """u16 length + utf-8 bytes, the framing every string field on the wire
    uses (session id, file relative paths)."""
    (n,) = struct.unpack(">H", recv_exact(sock, 2))
    return recv_exact(sock, n).decode("utf-8") if n else ""


def encode_handshake(version=PROTOCOL_VERSION):
    """Desktop -> phone greeting. A v1 app reads exactly these 6 bytes and
    never expects another inbound byte. Only send version=2 from a receiver
    that also speaks COMMAND/STATUS (rtvio.studio): a v2-aware app treats
    that version as "wait for a START command" instead of streaming."""
    return struct.pack(HANDSHAKE_FMT, HEADER_HANDSHAKE_ACK, version, STATUS_OK)


# JSON-bodied control packets (v2). u8 header, u16 length, UTF-8 JSON object.
# JSON rather than fixed structs because these carry a growing set of
# optional fields (capture settings, stats) and are sent a few times a
# second at most - the byte-exact fixed layouts above are for the hot path.
JSON_LEN_FMT = ">H"
MAX_JSON_BYTES = 65535


def _encode_json(header, obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_JSON_BYTES:
        raise ValueError("control packet body is %d bytes, max %d" % (len(body), MAX_JSON_BYTES))
    return bytes([header]) + struct.pack(JSON_LEN_FMT, len(body)) + body


def _read_json(sock):
    (n,) = struct.unpack(JSON_LEN_FMT, recv_exact(sock, 2))
    body = recv_exact(sock, n) if n else b"{}"
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise StreamClosed("malformed JSON control packet: %s" % e)
    if not isinstance(obj, dict):
        raise StreamClosed("control packet body is not a JSON object")
    return obj


def encode_command(obj):
    """Desktop -> phone. obj["cmd"] is one of "start", "stop", "ping"."""
    return _encode_json(HEADER_COMMAND, obj)


def encode_status(obj):
    """Phone -> desktop. Used by the phone simulator and the tests; the real
    encoder is Protocol.encodeStatus in the app."""
    return _encode_json(HEADER_STATUS, obj)


def read_command(sock):
    """Body of a COMMAND packet whose header byte was already consumed."""
    return _read_json(sock)


def read_status(sock):
    """Body of a STATUS packet whose header byte was already consumed."""
    return _read_json(sock)


# ------------------------------------------------------------------ decode --

def read_frame(sock):
    timestamp_ms, width, height, jpeg_size = struct.unpack(
        FRAME_HEADER_FMT, recv_exact(sock, FRAME_HEADER_BYTES))
    if not 0 < jpeg_size <= MAX_JPEG_BYTES:
        raise StreamClosed(f"implausible jpeg_size {jpeg_size}; stream desynchronised")
    if width <= 0 or height <= 0:
        raise StreamClosed(f"implausible frame size {width}x{height}; stream desynchronised")
    return FramePacket(timestamp_ms, width, height, recv_exact(sock, jpeg_size))


def read_imu(sock):
    (count,) = struct.unpack(IMU_COUNT_FMT, recv_exact(sock, 2))
    if not 0 <= count <= MAX_IMU_BATCH:
        raise StreamClosed(f"implausible IMU sample count {count}; stream desynchronised")
    payload = recv_exact(sock, count * IMU_SAMPLE_BYTES)
    out = []
    for i in range(count):
        t_ns, ax, ay, az, gx, gy, gz = struct.unpack_from(
            IMU_SAMPLE_FMT, payload, i * IMU_SAMPLE_BYTES)
        out.append(ImuSample(t_ns, (ax, ay, az), (gx, gy, gz)))
    return out


def read_gps(sock):
    return GpsPacket(*struct.unpack(GPS_FMT, recv_exact(sock, GPS_BYTES)))


def read_intrinsics(sock):
    """Read 0xFC intrinsics packet: 4 floats (K) + 5 doubles (distortion) + source string."""
    # 4 floats (fx, fy, cx, cy)
    k_floats = struct.unpack(">ffff", recv_exact(sock, 16))
    # 5 doubles (k1, k2, p1, p2, k3)
    distortion = struct.unpack(">ddddd", recv_exact(sock, 40))
    # source string: u16 length + bytes
    source_len_bytes = recv_exact(sock, 2)
    source_len = struct.unpack(">H", source_len_bytes)[0]
    source_bytes = recv_exact(sock, source_len) if source_len > 0 else b""
    source = source_bytes.decode("utf-8")

    return IntrinsicsPacket(
        fx_pix=k_floats[0],
        fy_pix=k_floats[1],
        cx_pix=k_floats[2],
        cy_pix=k_floats[3],
        k1=distortion[0],
        k2=distortion[1],
        p1=distortion[2],
        p2=distortion[3],
        k3=distortion[4],
        source=source
    )


def read_session_begin(sock):
    """Body of a SESSION_BEGIN packet whose header byte was already
    consumed: (session_id, file_count, total_bytes)."""
    session_id = read_pstring(sock)
    file_count, total_bytes = struct.unpack(">iq", recv_exact(sock, 12))
    return session_id, file_count, total_bytes


def read_session_file_header(sock):
    """Body of a SESSION_FILE packet whose header byte was already
    consumed: (relative_path, file_size) - the raw file bytes follow and
    are the caller's to read (recv_exact would buffer a large video/frame
    dump entirely in memory; the caller streams it to disk instead)."""
    rel_path = read_pstring(sock)
    (file_size,) = struct.unpack(">q", recv_exact(sock, 8))
    return rel_path, file_size


def read_packet(sock):
    """Read one packet. Returns (header_byte, payload) where payload is a
    FramePacket, a list[ImuSample], a GpsPacket, or an IntrinsicsPacket."""
    header = recv_exact(sock, 1)[0]
    if header == HEADER_FRAME:
        return header, read_frame(sock)
    if header == HEADER_IMU:
        return header, read_imu(sock)
    if header == HEADER_GPS:
        return header, read_gps(sock)
    if header == HEADER_INTRINSICS:
        return header, read_intrinsics(sock)
    if header == HEADER_STATUS:
        return header, read_status(sock)
    raise StreamClosed(f"unknown packet header 0x{header:02X}")


# ------------------------------------------------------------------ encode --
# Used by tools/replay_dataset_as_phone.py to stand in for the handset, and
# by the round-trip tests. Keeping encode and decode in one file is what
# makes "the test double speaks the same protocol" checkable rather than
# aspirational.

def encode_frame(pkt: FramePacket):
    return (bytes([HEADER_FRAME])
            + struct.pack(FRAME_HEADER_FMT, pkt.timestamp_ms, pkt.width,
                          pkt.height, len(pkt.jpeg))
            + pkt.jpeg)


def encode_imu_batch(samples):
    out = [bytes([HEADER_IMU]), struct.pack(IMU_COUNT_FMT, len(samples))]
    for s in samples:
        out.append(struct.pack(IMU_SAMPLE_FMT, s.t_ns, *s.accel, *s.gyro))
    return b"".join(out)


def encode_gps(pkt: GpsPacket):
    return bytes([HEADER_GPS]) + struct.pack(
        GPS_FMT, pkt.timestamp_ms, pkt.lat_deg, pkt.lon_deg,
        pkt.altitude_m, pkt.accuracy_m)
