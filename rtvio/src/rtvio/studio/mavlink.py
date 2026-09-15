"""
Minimal MAVLink v1/v2 codec for rtvio.studio's drone link (drone_link.py).

Covers only the common-dialect messages the Studio reads or sends. Message
ids, CRC_EXTRA bytes and field layouts are copied from the generated
mavlink20.js parser in the idronam branch's capture-bridge (itself extracted
from the iDronam GCS) - i.e. straight from MAVLink's common.xml, nothing
specific to one vendor's drone.

Each struct format below is in WIRE order: MAVLink serialises fields sorted
by type size (largest first) with extension fields appended at the end, so
it is not the order common.xml lists them in. A v1 frame, or a v2 frame
whose trailing zero bytes were truncated, is zero-padded before unpacking,
as the MAVLink 2 spec requires - which is also why extension fields
(GPS_RAW_INT.h_acc, ...) simply read as 0 when the sender did not fill them.

Deliberately not pymavlink: the Studio needs about ten messages, and this
keeps `python -m rtvio.studio` free of a dependency whose generated dialect
module alone is larger than the whole Studio.
"""
import re
import struct
from collections import namedtuple

STX_V1 = 0xFE
STX_V2 = 0xFD
IFLAG_SIGNED = 0x01
SIGNATURE_LEN = 13

MAV_TYPE_GCS = 6
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
MAV_AUTOPILOT_PX4 = 12
MAV_AUTOPILOT_INVALID = 8
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_DATA_STREAM_ALL = 0
MAV_CMD_SET_MESSAGE_INTERVAL = 511
MAV_CMD_REQUEST_MESSAGE = 512
MAV_COMP_ID_ALL = 0

# msgid: (name, crc_extra, struct format in wire order, field names in wire order).
# "name[n]" is an n-element array field.
MESSAGES = {
    0: ("HEARTBEAT", 50, "<IBBBBB",
        ["custom_mode", "type", "autopilot", "base_mode", "system_status", "mavlink_version"]),
    1: ("SYS_STATUS", 124, "<IIIHHhHHHHHHb",
        ["onboard_control_sensors_present", "onboard_control_sensors_enabled",
         "onboard_control_sensors_health", "load", "voltage_battery", "current_battery",
         "drop_rate_comm", "errors_comm", "errors_count1", "errors_count2", "errors_count3",
         "errors_count4", "battery_remaining"]),
    24: ("GPS_RAW_INT", 24, "<QiiiHHHHBBiIIIIH",
         ["time_usec", "lat", "lon", "alt", "eph", "epv", "vel", "cog", "fix_type",
          "satellites_visible", "alt_ellipsoid", "h_acc", "v_acc", "vel_acc", "hdg_acc", "yaw"]),
    30: ("ATTITUDE", 39, "<Iffffff",
         ["time_boot_ms", "roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed"]),
    33: ("GLOBAL_POSITION_INT", 104, "<IiiiihhhH",
         ["time_boot_ms", "lat", "lon", "alt", "relative_alt", "vx", "vy", "vz", "hdg"]),
    66: ("REQUEST_DATA_STREAM", 148, "<HBBBB",
         ["req_message_rate", "target_system", "target_component", "req_stream_id", "start_stop"]),
    74: ("VFR_HUD", 20, "<ffffhH",
         ["airspeed", "groundspeed", "alt", "climb", "heading", "throttle"]),
    76: ("COMMAND_LONG", 152, "<fffffffHBBB",
         ["param1", "param2", "param3", "param4", "param5", "param6", "param7",
          "command", "target_system", "target_component", "confirmation"]),
    147: ("BATTERY_STATUS", 154, "<iih10HhBBBb",
          ["current_consumed", "energy_consumed", "temperature", "voltages[10]",
           "current_battery", "id", "battery_function", "type", "battery_remaining"]),
    253: ("STATUSTEXT", 83, "<B50s", ["severity", "text"]),
    # Camera protocol - only answered by a drone whose camera speaks MAVLink
    # (a camera manager on the companion computer, or an autopilot camera
    # backend); drone_link asks for them and records whatever comes back.
    259: ("CAMERA_INFORMATION", 92, "<IIfffIHHH32s32sB140s",
          ["time_boot_ms", "firmware_version", "focal_length", "sensor_size_h", "sensor_size_v",
           "flags", "resolution_h", "resolution_v", "cam_definition_version", "vendor_name",
           "model_name", "lens_id", "cam_definition_uri"]),
    269: ("VIDEO_STREAM_INFORMATION", 109, "<fIHHHHHBBB32s160s",
          ["framerate", "bitrate", "flags", "resolution_h", "resolution_v", "rotation", "hfov",
           "stream_id", "count", "type", "name", "uri"]),
}
IDS = {spec[0]: msgid for msgid, spec in MESSAGES.items()}

Message = namedtuple("Message", "name id sysid compid seq fields")


def x25_crc(data, crc=0xFFFF):
    """CRC-16/MCRF4XX, which MAVLink calls X.25 (check value 0x6F91)."""
    for b in data:
        tmp = (b ^ crc) & 0xFF
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _array_field(name):
    key, n = name[:-1].split("[")
    return key, int(n)


def _codes(fmt):
    """struct codes of fmt, one per field name ("10H" is one array field,
    "32s" one string field)."""
    return re.findall(r"\d*([a-zA-Z])", fmt[1:])


def decode_payload(msgid, payload):
    _name, _crc, fmt, names = MESSAGES[msgid]
    size = struct.calcsize(fmt)
    vals = struct.unpack(fmt, bytes(payload[:size]).ljust(size, b"\0"))
    out, i = {}, 0
    for f in names:
        if f.endswith("]"):
            key, n = _array_field(f)
            out[key] = list(vals[i:i + n])
            i += n
        else:
            v = vals[i]
            i += 1
            out[f] = v.split(b"\0", 1)[0].decode("utf-8", "replace") if isinstance(v, bytes) else v
    return out


def encode(msg_name, seq=0, sysid=255, compid=1, **fields):
    """One unsigned MAVLink 2 frame. Unset fields are 0 (b"" for strings);
    trailing zero bytes of the payload are truncated (keeping at least one),
    as MAVLink 2 asks senders to. msg_name, not name: VIDEO_STREAM_INFORMATION
    has a field called "name"."""
    msgid = IDS[msg_name]
    _name, crc_extra, fmt, names = MESSAGES[msgid]
    vals = []
    for f, code in zip(names, _codes(fmt)):
        if f.endswith("]"):
            key, n = _array_field(f)
            arr = list(fields.get(key, ()))[:n]
            vals.extend(arr + [0] * (n - len(arr)))
        else:
            v = fields.get(f, b"" if code == "s" else 0)
            vals.append(v.encode("utf-8") if isinstance(v, str) else v)
    payload = struct.pack(fmt, *vals).rstrip(b"\0") or b"\0"
    header = bytes((len(payload), 0, 0, seq & 0xFF, sysid, compid,
                    msgid & 0xFF, (msgid >> 8) & 0xFF, (msgid >> 16) & 0xFF))
    crc = x25_crc(bytes((crc_extra,)), x25_crc(header + payload))
    return bytes((STX_V2,)) + header + payload + struct.pack("<H", crc)


class Parser:
    """Incremental frame parser: feed() it whatever recv() returned and get
    back every complete, CRC-valid frame of a message in MESSAGES.

    A frame of any other message cannot be CRC-checked (its CRC_EXTRA is not
    known here), so its length field is trusted and it is skipped whole -
    provided the byte right after it is another start marker (or not here
    yet), which it always is on a clean link where frames are back to back.
    A known message failing its CRC, or an unknown one not followed by a
    start marker, means the parser is misaligned: drop one byte and resync
    on the next marker."""

    def __init__(self):
        self.buf = bytearray()
        self.frames = 0
        self.unknown = 0
        self.crc_errors = 0

    def feed(self, data):
        buf = self.buf
        buf += data
        out = []
        while buf:
            starts = [i for i in (buf.find(STX_V2), buf.find(STX_V1)) if i >= 0]
            if not starts:
                buf.clear()
                break
            if min(starts):
                del buf[:min(starts)]
            if buf[0] == STX_V2:
                if len(buf) < 10:
                    break
                plen = buf[1]
                total = 12 + plen + (SIGNATURE_LEN if buf[2] & IFLAG_SIGNED else 0)
                seq, sysid, compid = buf[4], buf[5], buf[6]
                msgid = buf[7] | (buf[8] << 8) | (buf[9] << 16)
                body = 10
            else:
                if len(buf) < 6:
                    break
                plen = buf[1]
                total = 8 + plen
                seq, sysid, compid, msgid = buf[2], buf[3], buf[4], buf[5]
                body = 6
            if len(buf) < total:
                break
            spec = MESSAGES.get(msgid)
            if spec is None:
                if len(buf) > total and buf[total] not in (STX_V1, STX_V2):
                    self.crc_errors += 1
                    del buf[:1]
                    continue
                self.unknown += 1
                del buf[:total]
                continue
            end = body + plen
            crc = x25_crc(bytes((spec[1],)), x25_crc(buf[1:end]))
            if crc != (buf[end] | (buf[end + 1] << 8)):
                self.crc_errors += 1
                del buf[:1]
                continue
            out.append(Message(spec[0], msgid, sysid, compid, seq,
                               decode_payload(msgid, buf[body:end])))
            self.frames += 1
            del buf[:total]
        return out
