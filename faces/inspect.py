"""Everything one photo gives away. Local, offline by default.

    python -m faces.inspect photo.jpg            # human-readable report
    python -m faces.inspect photo.jpg --json     # machine-readable
    python -m faces.inspect photo.jpg --geocode  # + reverse-geocode GPS (OpenStreetMap, network)
    python -m faces.inspect photo.jpg --describe # + what a vision model reads off the pixels (Claude, network)
    python -m faces.inspect photo.jpg --out DIR  # also save the report and any embedded thumbnail

Modelled on theyseeyourphotos.com (Ente), which sends the photo to Google and
has a model write a profile. This does the metadata half entirely on your machine
and the pixel half only when you ask. Deliberately absent: guesses at ethnicity,
religion, politics, orientation, income or health. Those are not in the photo;
they are in the model.

Layers, in order of how hard they are to fake:
  1. file      — bytes, hashes, container, segment fingerprint (which software touched it)
  2. exif      — the camera's own record: device, lens, serials, exposure, timestamps, GPS
  3. xmp/iptc  — editor and agency metadata: creator, keywords, edit history, people tags
  4. thumbnail — the EXIF preview is written at capture and often survives crops and edits
  5. filename  — IMG_, PXL_, -WA, Screenshot_ … patterns name the app that produced the file
  6. faces     — count, size, estimated age and gender, head pose, blur (local models)
  7. describe  — optional: scene, place and time clues, text, objects, exposure risks
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import struct
import sys
from pathlib import Path

log = logging.getLogger("faces.inspect")

# ----------------------------------------------------------------------------- helpers

def _ratio(v):
    """Pillow returns IFDRational; make it JSON-friendly and keep precision."""
    try:
        from PIL.TiffImagePlugin import IFDRational
        if isinstance(v, IFDRational):
            return float(v) if v.denominator else None
    except ImportError:
        pass
    return v


def _jsonable(v):
    if isinstance(v, bytes):
        if len(v) > 64:
            return f"<{len(v)} bytes>"
        try:
            return v.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            return v.hex()
    if isinstance(v, (tuple, list)):
        return [_jsonable(_ratio(x)) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    v = _ratio(v)
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def _dms_to_deg(dms, ref):
    try:
        d, m, s = (float(_ratio(x)) for x in dms)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    return -deg if ref in ("S", "W") else deg


def _parse_exif_dt(s: str | None):
    if not s or not isinstance(s, str):
        return None
    s = s.strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------------- 1. file

MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),  # checked further below
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"BM", "image/bmp"),
]


def sniff_mime(head: bytes) -> str | None:
    for magic, mime in MAGIC:
        if head.startswith(magic):
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return mime
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        return "video/mp4"
    return None


JPEG_APP_IDS = {
    b"JFIF": "JFIF", b"JFXX": "JFIF ext", b"Exif": "EXIF", b"http://ns.adobe.com/xap/1.0/": "XMP",
    b"http://ns.adobe.com/xmp/extension/": "Extended XMP", b"ICC_PROFILE": "ICC profile",
    b"Photoshop 3.0": "Photoshop IRB (IPTC)", b"Adobe": "Adobe APP14", b"MPF": "MPF (multi-picture; Apple/Samsung)",
    b"Ducky": "Adobe 'Save for Web'", b"http://ns.google.com/photos/1.0/camera/": "Google camera XMP",
}


def jpeg_segments(data: bytes) -> list[dict]:
    """Walk the JPEG marker chain up to SOS. Which APPn segments exist, in which order,
    is a fingerprint of the software that last wrote the file."""
    out = []
    if not data.startswith(b"\xff\xd8"):
        return out
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # EOI / SOS: entropy-coded data follows
            out.append({"marker": f"0x{marker:02X}", "name": "SOS" if marker == 0xDA else "EOI", "offset": i})
            break
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        seg = data[i + 4:i + 2 + length]
        name = {0xDB: "DQT", 0xC4: "DHT", 0xC0: "SOF0 baseline", 0xC1: "SOF1", 0xC2: "SOF2 progressive",
                0xFE: "COM comment", 0xDD: "DRI"}.get(marker)
        ident = None
        if 0xE0 <= marker <= 0xEF:
            name = f"APP{marker - 0xE0}"
            for key, label in JPEG_APP_IDS.items():
                if seg.startswith(key):
                    ident = label
                    break
            if ident is None:
                ident = seg.split(b"\x00", 1)[0][:24].decode("latin-1", "replace")
        entry = {"marker": f"0x{marker:02X}", "name": name or f"0x{marker:02X}", "length": length, "offset": i}
        if ident:
            entry["id"] = ident
        if marker == 0xFE:
            entry["text"] = seg.decode("utf-8", "replace").rstrip("\x00")[:500]
        out.append(entry)
        i += 2 + length
    return out


def estimate_jpeg_quality(im) -> int | None:
    """Approximate libjpeg quality from the luma quantization table (IJG scaling)."""
    q = getattr(im, "quantization", None)
    if not q or 0 not in q:
        return None
    table = q[0]
    if len(table) < 64:
        return None
    # IJG standard luminance table, zig-zag order as Pillow stores it
    std = [16, 11, 12, 14, 12, 10, 16, 14, 13, 14, 18, 17, 16, 19, 24, 40, 26, 24, 22, 22, 24, 49, 35, 37, 29, 40,
           58, 51, 61, 60, 57, 51, 56, 55, 64, 72, 92, 78, 64, 68, 87, 69, 55, 56, 80, 109, 81, 87, 95, 98, 103,
           104, 103, 62, 77, 113, 121, 112, 100, 120, 92, 101, 103, 99]
    scale = sum(table) / sum(std) * 100
    quality = 5000 / scale if scale > 100 else 100 - scale / 2
    return int(round(max(1, min(100, quality))))


def file_layer(path: Path, data: bytes, im) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    md5 = hashlib.md5(data).hexdigest()
    out = {
        "name": path.name,
        "bytes": len(data),
        "mime_sniffed": sniff_mime(data[:16]),
        "pil_format": im.format,
        "mode": im.mode,
        "width": im.width,
        "height": im.height,
        "megapixels": round(im.width * im.height / 1e6, 2),
        "aspect": round(im.width / im.height, 4) if im.height else None,
        "sha256": sha,
        "md5": md5,
        "modified_on_disk": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "dpi": _jsonable(im.info.get("dpi")),
        "progressive": bool(im.info.get("progressive") or im.info.get("progression")),
    }
    icc = im.info.get("icc_profile")
    if icc:
        out["icc_profile_bytes"] = len(icc)
        try:
            from PIL import ImageCms
            out["icc_description"] = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
        except Exception:  # noqa: BLE001 — profile description is a nicety
            pass
    if im.format == "JPEG":
        out["jpeg_quality_estimate"] = estimate_jpeg_quality(im)
        segs = jpeg_segments(data)
        out["jpeg_segments"] = [{k: v for k, v in s.items() if k != "offset"} for s in segs]
        comments = [s["text"] for s in segs if s.get("text")]
        if comments:
            out["comments"] = comments
        out["subsampling"] = _subsampling(im)
    if im.format == "PNG":
        text = {k: v for k, v in im.info.items() if isinstance(v, str) and k not in ("icc_profile",)}
        if text:
            out["png_text_chunks"] = {k: v[:500] for k, v in text.items()}
    return out


def _subsampling(im):
    try:
        from PIL import JpegImagePlugin
        return {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}.get(JpegImagePlugin.get_sampling(im), "unknown")
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------- 2. exif

def exif_layer(im) -> dict:
    from PIL import ExifTags
    from PIL.ExifTags import GPSTAGS, TAGS

    exif = im.getexif()
    if not exif:
        return {}
    out: dict = {}

    def dump(ifd, names):
        d = {}
        for tag, val in ifd.items():
            name = names.get(tag, f"Tag0x{tag:04X}")
            if name in ("MakerNote", "UserComment") and isinstance(val, bytes):
                d[name] = f"<{len(val)} bytes>" if name == "MakerNote" else _decode_user_comment(val)
                continue
            d[name] = _jsonable(val)
        return d

    out["IFD0"] = dump(exif, TAGS)
    out["IFD0"].pop("ExifOffset", None)
    out["IFD0"].pop("GPSInfo", None)
    for ifd_id, key, names in ((ExifTags.IFD.Exif, "Exif", TAGS), (ExifTags.IFD.GPSInfo, "GPS", GPSTAGS),
                               (ExifTags.IFD.Interop, "Interop", TAGS), (ExifTags.IFD.IFD1, "IFD1_thumbnail", TAGS)):
        try:
            sub = exif.get_ifd(ifd_id)
        except Exception:  # noqa: BLE001 — a corrupt sub-IFD should not kill the report
            continue
        if sub:
            out[key] = dump(sub, names)
    try:
        mn = exif.get_ifd(ExifTags.IFD.MakerNote)
        if mn:
            out["MakerNote_parsed_tags"] = len(mn)
    except Exception:  # noqa: BLE001
        pass
    return out


def _decode_user_comment(val: bytes) -> str:
    code, body = val[:8], val[8:]
    if code.startswith(b"ASCII"):
        return body.decode("ascii", "replace").rstrip("\x00 ")
    if code.startswith(b"UNICODE"):
        for enc in ("utf-16-be", "utf-16-le"):
            try:
                return body.decode(enc).rstrip("\x00 ")
            except UnicodeDecodeError:
                continue
    return body.decode("utf-8", "replace").rstrip("\x00 ")


FLASH_BITS = {0: "no flash", 1: "fired", 5: "fired, no return", 7: "fired, return", 8: "on, did not fire",
              9: "on, fired", 16: "off, did not fire", 24: "auto, did not fire", 25: "auto, fired",
              32: "no flash function", 65: "fired, red-eye", 89: "auto, fired, red-eye"}
EXPOSURE_PROGRAM = {0: "undefined", 1: "manual", 2: "program", 3: "aperture priority", 4: "shutter priority",
                    5: "creative", 6: "action", 7: "portrait", 8: "landscape"}
SCENE_TYPE = {0: "standard", 1: "landscape", 2: "portrait", 3: "night"}
ORIENTATION = {1: "normal", 2: "mirrored", 3: "rotated 180", 4: "mirrored, rotated 180", 5: "mirrored, rotated 90 CCW",
               6: "rotated 90 CW", 7: "mirrored, rotated 90 CW", 8: "rotated 90 CCW"}


def derive(exif: dict, xmp: dict) -> dict:
    """Turn raw tags into the facts someone would actually use."""
    ifd0, ex, gps = exif.get("IFD0", {}), exif.get("Exif", {}), exif.get("GPS", {})
    d: dict = {}

    # --- device
    make, model = (ifd0.get("Make") or "").strip(), (ifd0.get("Model") or "").strip()
    if make or model:
        d["camera"] = f"{make} {model}".strip() if not model.startswith(make) else model
    lens = ex.get("LensModel") or xmp.get("aux:Lens") or xmp.get("exifEX:LensModel")
    if lens:
        d["lens"] = str(lens).strip()
    for src, key in ((ex, "BodySerialNumber"), (ex, "CameraSerialNumber"), (xmp, "aux:SerialNumber")):
        if src.get(key):
            d["body_serial"] = str(src[key]).strip()
            break
    for src, key in ((ex, "LensSerialNumber"), (xmp, "aux:LensSerialNumber")):
        if src.get(key):
            d["lens_serial"] = str(src[key]).strip()
            break
    if ifd0.get("Software"):
        d["software"] = str(ifd0["Software"]).strip()
    if ifd0.get("HostComputer"):
        d["host_computer"] = str(ifd0["HostComputer"]).strip()
    if ex.get("ImageUniqueID"):
        d["image_unique_id"] = ex["ImageUniqueID"]
    if ifd0.get("Orientation"):
        d["orientation"] = ORIENTATION.get(ifd0["Orientation"], ifd0["Orientation"])

    # --- people / ownership written into the file
    owner = {}
    for key in ("Artist", "Copyright", "ImageDescription", "XPAuthor", "XPTitle", "XPComment", "XPKeywords", "XPSubject"):
        if ifd0.get(key):
            owner[key] = _xp(ifd0[key])
    for key in ("CameraOwnerName", "UserComment"):
        if ex.get(key):
            owner[key] = ex[key]
    if owner:
        d["owner_fields"] = owner

    # --- exposure
    exp = {}
    t = ex.get("ExposureTime")
    if (t is None or t <= 0) and ex.get("ShutterSpeedValue") is not None:
        t = 2 ** (-float(ex["ShutterSpeedValue"]))  # APEX Tv → seconds
    if t:
        exp["shutter"] = f"1/{round(1 / t)} s" if t < 1 else f"{t:g} s"
    fn = ex.get("FNumber")
    if (not fn or fn < 0.9) and ex.get("ApertureValue") is not None:
        fn = 2 ** (float(ex["ApertureValue"]) / 2)  # APEX Av → f-number; some writers store junk in FNumber
    if fn:
        exp["aperture"] = f"f/{fn:.3g}"
    iso = ex.get("ISOSpeedRatings") or ex.get("PhotographicSensitivity")
    if iso:
        exp["iso"] = iso[0] if isinstance(iso, list) else iso
    if ex.get("FocalLength"):
        exp["focal_length_mm"] = round(float(ex["FocalLength"]), 2)
    if ex.get("FocalLengthIn35mmFilm"):
        exp["focal_length_35mm_equiv"] = ex["FocalLengthIn35mmFilm"]
    if ex.get("Flash") is not None:
        exp["flash"] = FLASH_BITS.get(ex["Flash"], f"0x{ex['Flash']:02X}")
    if ex.get("ExposureProgram") is not None:
        exp["program"] = EXPOSURE_PROGRAM.get(ex["ExposureProgram"], ex["ExposureProgram"])
    if ex.get("SceneCaptureType") is not None:
        exp["scene"] = SCENE_TYPE.get(ex["SceneCaptureType"], ex["SceneCaptureType"])
    if ex.get("ExposureBiasValue") is not None:
        exp["exposure_bias_ev"] = round(float(ex["ExposureBiasValue"]), 2)
    if ex.get("DigitalZoomRatio"):
        exp["digital_zoom"] = ex["DigitalZoomRatio"]
    if exp:
        d["exposure"] = exp

    # --- time
    times = {}
    for key, label in (("DateTimeOriginal", "taken"), ("DateTimeDigitized", "digitized"), ("DateTime", "modified")):
        src = ex if key != "DateTime" else ifd0
        when = _parse_exif_dt(src.get(key))
        if when:
            off = ex.get({"DateTimeOriginal": "OffsetTimeOriginal", "DateTimeDigitized": "OffsetTimeDigitized",
                          "DateTime": "OffsetTime"}[key])
            sub = ex.get({"DateTimeOriginal": "SubsecTimeOriginal", "DateTimeDigitized": "SubsecTimeDigitized",
                          "DateTime": "SubsecTime"}[key])
            s = when.isoformat()
            if sub:
                s += f".{str(sub).strip()}"
            if off:
                s += str(off).strip()
            times[label] = s
    gps_utc = None
    if gps.get("GPSDateStamp") and gps.get("GPSTimeStamp"):
        try:
            h, m, sec = (float(x) for x in gps["GPSTimeStamp"])
            day = dt.datetime.strptime(str(gps["GPSDateStamp"]).strip()[:10], "%Y:%m:%d")
            gps_utc = day.replace(hour=int(h), minute=int(m), second=int(sec), tzinfo=dt.timezone.utc)
            times["gps_utc"] = gps_utc.isoformat()
        except (ValueError, TypeError):
            pass
    taken = _parse_exif_dt(ex.get("DateTimeOriginal"))
    if taken and gps_utc and not ex.get("OffsetTimeOriginal"):
        delta = (taken - gps_utc.replace(tzinfo=None)).total_seconds()
        hours = round(delta / 900) * 0.25  # nearest quarter hour
        if abs(hours) <= 14:
            sign = "+" if hours >= 0 else "-"
            times["timezone_inferred"] = f"UTC{sign}{int(abs(hours)):02d}:{int(abs(hours) % 1 * 60):02d}"
    if taken:
        times["weekday"] = taken.strftime("%A")
    if times:
        d["time"] = times

    # --- place
    if gps:
        lat = _dms_to_deg(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
        lon = _dms_to_deg(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
        place = {}
        if lat is not None and lon is not None:
            place["latitude"] = round(lat, 6)
            place["longitude"] = round(lon, 6)
            place["maps"] = f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"
            place["osm"] = f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}"
        if gps.get("GPSAltitude") is not None:
            alt = float(gps["GPSAltitude"])
            if gps.get("GPSAltitudeRef") in (1, b"\x01", "\x01"):
                alt = -alt
            place["altitude_m"] = round(alt, 1)
        if gps.get("GPSImgDirection") is not None:
            place["camera_heading_deg"] = round(float(gps["GPSImgDirection"]), 1)
            place["heading_ref"] = {"T": "true north", "M": "magnetic"}.get(gps.get("GPSImgDirectionRef"))
        if gps.get("GPSSpeed") is not None:
            unit = {"K": "km/h", "M": "mph", "N": "knots"}.get(gps.get("GPSSpeedRef"), "")
            place["speed"] = f"{float(gps['GPSSpeed']):g} {unit}".strip()
        if gps.get("GPSHPositioningError") is not None:
            place["horizontal_error_m"] = round(float(gps["GPSHPositioningError"]), 1)
        if gps.get("GPSDestLatitude"):
            place["destination"] = [_dms_to_deg(gps["GPSDestLatitude"], gps.get("GPSDestLatitudeRef")),
                                    _dms_to_deg(gps.get("GPSDestLongitude"), gps.get("GPSDestLongitudeRef"))]
        if gps.get("GPSProcessingMethod"):
            place["processing_method"] = _decode_user_comment(gps["GPSProcessingMethod"].encode("latin-1")) \
                if isinstance(gps["GPSProcessingMethod"], str) else gps["GPSProcessingMethod"]
        if place:
            d["place"] = place
    else:
        # GPS can also hide in XMP after an editor strips EXIF GPS
        xlat, xlon = xmp.get("exif:GPSLatitude"), xmp.get("exif:GPSLongitude")
        if xlat and xlon:
            d["place"] = {"xmp_gps": [xlat, xlon]}
    return d


def _xp(v):
    """Windows XP* tags are UCS-2 stored as a byte list."""
    if isinstance(v, (list, tuple)) and v and all(isinstance(x, int) for x in v):
        try:
            return bytes(v).decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            return v
    return v


# ----------------------------------------------------------------------------- 3. xmp / iptc

XMP_INTEREST = {
    "dc:creator": "creator", "dc:rights": "rights", "dc:subject": "keywords", "dc:description": "description",
    "dc:title": "title", "xmp:CreatorTool": "software", "xmp:CreateDate": "create_date", "xmp:ModifyDate": "modify_date",
    "xmp:MetadataDate": "metadata_date", "xmp:Rating": "rating", "photoshop:DateCreated": "date_created",
    "photoshop:City": "city", "photoshop:State": "state", "photoshop:Country": "country", "photoshop:Credit": "credit",
    "photoshop:Source": "source", "photoshop:Headline": "headline", "photoshop:Instructions": "instructions",
    "Iptc4xmpCore:Location": "sublocation", "Iptc4xmpExt:PersonInImage": "person_in_image",
    "Iptc4xmpExt:LocationShown": "location_shown", "Iptc4xmpCore:CreatorContactInfo": "creator_contact",
    "Iptc4xmpCore:CiEmailWork": "creator_email", "Iptc4xmpCore:CiTelWork": "creator_phone",
    "Iptc4xmpCore:CiAdrCity": "creator_city", "Iptc4xmpCore:CiUrlWork": "creator_url",
    "xmpMM:DocumentID": "document_id", "xmpMM:OriginalDocumentID": "original_document_id",
    "xmpMM:InstanceID": "instance_id", "xmpMM:DerivedFrom": "derived_from",
    "stEvt:softwareAgent": "edit_history_software", "stEvt:action": "edit_history_actions", "stEvt:when": "edit_history_when",
    "aux:Lens": "lens", "aux:SerialNumber": "body_serial", "aux:LensSerialNumber": "lens_serial",
    "aux:Firmware": "firmware", "aux:OwnerName": "owner_name", "aux:ImageNumber": "shutter_count",
    "crs:RawFileName": "raw_file_name", "crs:Version": "camera_raw_version", "crs:HasCrop": "cropped",
    "crs:CropTop": "crop_top", "crs:CropLeft": "crop_left", "crs:CropBottom": "crop_bottom", "crs:CropRight": "crop_right",
    "crs:Exposure2012": "lr_exposure", "crs:AlreadyApplied": "lr_already_applied",
    "mwg-rs:Name": "face_region_names", "MPReg:PersonDisplayName": "ms_people_tags",
    "GCamera:MicroVideo": "google_motion_photo", "GCamera:SpecialTypeID": "google_special_type",
    "GDepth:Format": "google_depth_map", "apple-fi:AngleInfoRoll": "apple_face_info",
    "exif:GPSLatitude": "xmp_gps_latitude", "exif:GPSLongitude": "xmp_gps_longitude",
    "Iptc4xmpExt:DigitalSourceType": "digital_source_type",  # e.g. trainedAlgorithmicMedia = AI generated
    "xmpDM:shotName": "shot_name", "tiff:Make": "tiff_make", "tiff:Model": "tiff_model",
}


def find_xmp_packets(data: bytes) -> list[bytes]:
    pk = []
    for m in re.finditer(rb"<x:xmpmeta[\s\S]*?</x:xmpmeta>", data):
        pk.append(m.group(0))
    if not pk:
        for m in re.finditer(rb"<rdf:RDF[\s\S]*?</rdf:RDF>", data):
            pk.append(m.group(0))
    return pk


def flatten_xmp(packet: bytes) -> dict:
    """Dependency-free flattening of an XMP packet into {prefix:Name: value|[values]}.
    Regex, not a full XML parser: XMP in the wild is often truncated or malformed."""
    text = packet.decode("utf-8", "replace")
    flat: dict = {}

    def put(k, v):
        v = v.strip()
        if not v or k.startswith(("xmlns:", "rdf:", "x:")):
            return
        if k in flat:
            cur = flat[k]
            if isinstance(cur, list):
                if v not in cur:
                    cur.append(v)
            elif cur != v:
                flat[k] = [cur, v]
        else:
            flat[k] = v

    # attribute form: <rdf:Description xmp:CreatorTool="…" …>
    for m in re.finditer(r'([A-Za-z][\w\-]*:[A-Za-z][\w\-]*)="([^"]*)"', text):
        put(m.group(1), _unescape(m.group(2)))
    # element form: <xmp:CreatorTool>…</xmp:CreatorTool>, including <rdf:li> items inside
    for m in re.finditer(r"<([A-Za-z][\w\-]*:[A-Za-z][\w\-]*)(?:\s[^>]*)?>([\s\S]*?)</\1>", text):
        k, body = m.group(1), m.group(2)
        items = re.findall(r"<rdf:li(?:\s[^>]*)?>([\s\S]*?)</rdf:li>", body)
        if items:
            for it in items:
                if "<" not in it:
                    put(k, _unescape(it))
        elif "<" not in body:
            put(k, _unescape(body))
    return flat


def _unescape(s: str) -> str:
    import html
    return html.unescape(s)


def xmp_layer(data: bytes, im) -> tuple[dict, dict]:
    packets = find_xmp_packets(data)
    xmp_bytes = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    if xmp_bytes and isinstance(xmp_bytes, (bytes, str)):
        b = xmp_bytes if isinstance(xmp_bytes, bytes) else xmp_bytes.encode()
        if b not in packets:
            packets.append(b)
    flat: dict = {}
    for p in packets:
        for k, v in flatten_xmp(p).items():
            flat.setdefault(k, v)
    interesting = {label: flat[k] for k, label in XMP_INTEREST.items() if k in flat and _nonblank(flat[k])}
    return flat, interesting


def _nonblank(v) -> bool:
    if isinstance(v, list):
        return any(_nonblank(x) for x in v)
    return bool(str(v).strip()) if v is not None else False


IPTC_NAMES = {
    (2, 5): "object_name", (2, 10): "urgency", (2, 15): "category", (2, 20): "supplemental_categories",
    (2, 25): "keywords", (2, 40): "special_instructions", (2, 55): "date_created", (2, 60): "time_created",
    (2, 80): "by_line", (2, 85): "by_line_title", (2, 90): "city", (2, 92): "sublocation", (2, 95): "province_state",
    (2, 100): "country_code", (2, 101): "country", (2, 103): "transmission_reference", (2, 105): "headline",
    (2, 110): "credit", (2, 115): "source", (2, 116): "copyright", (2, 120): "caption", (2, 122): "caption_writer",
}


def iptc_layer(im) -> dict:
    try:
        from PIL import IptcImagePlugin
        raw = IptcImagePlugin.getiptcinfo(im) or {}
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for key, val in raw.items():
        name = IPTC_NAMES.get(key)
        if not name:
            continue
        dec = [v.decode("utf-8", "replace").strip() for v in val] if isinstance(val, list) else val.decode("utf-8", "replace").strip()
        if _nonblank(dec):
            out[name] = dec
    return out


# ----------------------------------------------------------------------------- 4. thumbnail

def thumbnail_layer(data: bytes, im, out_dir: Path | None) -> dict:
    """The EXIF thumbnail is written by the camera and many editors never refresh it.
    When it disagrees with the main image, the photo was cropped, flipped, retouched,
    or is a different picture altogether."""
    from PIL import Image

    exif_blob = im.info.get("exif")
    if not exif_blob:
        return {}
    start = exif_blob.find(b"\xff\xd8\xff")
    end = exif_blob.rfind(b"\xff\xd9")
    if start < 0 or end < start:
        return {}
    thumb_bytes = exif_blob[start:end + 2]
    out = {"bytes": len(thumb_bytes)}
    try:
        th = Image.open(io.BytesIO(thumb_bytes))
        th.load()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"undecodable: {e}"
        return out
    out["width"], out["height"] = th.size
    try:
        from PIL import ImageOps
        a = ImageOps.exif_transpose(im) if im.getexif().get(0x0112) else im
        side = 32
        ga = ImageOps.fit(a.convert("L"), (side, side))
        gb = ImageOps.fit(th.convert("L"), (side, side))
        pa, pb = list(ga.getdata()), list(gb.getdata())
        ma, mb = sum(pa) / len(pa), sum(pb) / len(pb)
        pa = [p - ma for p in pa]
        pb = [p - mb for p in pb]
        num = sum(x * y for x, y in zip(pa, pb))
        den = (sum(x * x for x in pa) * sum(y * y for y in pb)) ** 0.5 or 1
        corr = num / den
        out["similarity_to_image"] = round(corr, 3)
        out["aspect_matches"] = abs(th.width / th.height - im.width / im.height) < 0.05 if th.height and im.height else None
        out["verdict"] = ("matches image" if corr > 0.9 and out["aspect_matches"]
                          else "differs — image was likely cropped, rotated or edited after capture" if corr > 0.5
                          else "different picture — thumbnail is of another image")
    except Exception as e:  # noqa: BLE001
        out["compare_error"] = str(e)
    if out_dir:
        p = out_dir / "exif_thumbnail.jpg"
        p.write_bytes(thumb_bytes)
        out["saved_to"] = str(p)
    return out


# ----------------------------------------------------------------------------- 5. filename

FILENAME_PATTERNS = [
    (r"^IMG_\d{4}\.(JPG|HEIC|jpg|heic)$", "Apple iPhone/iPad camera roll (IMG_nnnn)"),
    (r"^IMG_E\d{4}", "Apple, edited copy (IMG_Ennnn)"),
    (r"^IMG_\d{8}_\d{6}", "Android camera (IMG_yyyymmdd_hhmmss)"),
    (r"^PXL_\d{8}_\d{9}", "Google Pixel camera (PXL_)"),
    (r"^IMG-\d{8}-WA\d{4}", "Received via WhatsApp (IMG-date-WAnnnn) — metadata stripped by WhatsApp"),
    (r"^VID-\d{8}-WA", "WhatsApp video"),
    (r"^PHOTO-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}", "Exported from WhatsApp on iOS"),
    (r"^signal-\d{4}-\d{2}-\d{2}", "Signal messenger"),
    (r"^FB_IMG_\d+", "Saved from Facebook app"),
    (r"^received_\d+", "Saved from Facebook Messenger"),
    (r"^Screenshot[_ -]", "Screenshot"),
    (r"^Screen Shot \d{4}", "macOS screenshot"),
    (r"^DSC_\d{4}", "Nikon camera (DSC_)"),
    (r"^DSC\d{5}", "Sony camera (DSCnnnnn)"),
    (r"^_DSC\d{4}", "Nikon/Sony, AdobeRGB colour space (_DSC)"),
    (r"^DSCF\d{4}", "Fujifilm camera (DSCF)"),
    (r"^_MG_\d{4}|^IMG_\d{4}\.CR\d", "Canon camera"),
    (r"^P\d{7}\.", "Panasonic/Olympus camera"),
    (r"^GOPR\d{4}|^GX\d{6}", "GoPro"),
    (r"^DJI_\d{4}", "DJI drone/camera"),
    (r"^\d{8}_\d{6}\.(jpg|JPG|heic)", "Samsung camera (yyyymmdd_hhmmss)"),
    (r"^image\d*\.(jpeg|jpg|png)$", "Pasted/exported image (image.jpeg) — iOS share sheet or clipboard"),
    (r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "UUID name — web upload, Telegram or app export"),
    (r"^photo_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", "Telegram desktop export"),
    (r"^Snapchat-\d+", "Snapchat"),
    (r"^[A-Za-z0-9_-]+-unsplash\.jpg$", "Downloaded from Unsplash (photographer slug in name)"),
    (r"^\d{13}\.(jpg|png)$", "Unix-millisecond timestamp name — app export"),
]


def filename_layer(name: str, has_exif: bool, mime: str | None) -> dict:
    hints = [label for pat, label in FILENAME_PATTERNS if re.search(pat, name)]
    out = {"hints": hints}
    if not has_exif:
        if mime == "image/png":
            out["note"] = "PNG with no EXIF — a screenshot, a render, or an export; cameras do not produce PNG"
        elif mime == "image/webp":
            out["note"] = "WebP — re-encoded by a website or messaging app; original metadata gone"
        else:
            out["note"] = "No EXIF — stripped by a social network / messenger, or a screenshot"
    return out


# ----------------------------------------------------------------------------- 6. faces

def faces_layer(im, limit: int = 50) -> dict:
    """Count and characterise faces with the same InsightFace pack the indexer uses,
    plus its age/gender and 3-D landmark heads (not loaded by the indexer)."""
    import numpy as np
    from PIL import ImageOps

    from .models import _cap_ort_threads, _cpu_budget, _providers, blur_score
    from .config import MODELS, cfg

    _cap_ort_threads(_cpu_budget())
    from insightface.app import FaceAnalysis
    MODELS.mkdir(parents=True, exist_ok=True)
    app = FaceAnalysis(name="buffalo_l", root=str(MODELS), providers=_providers())
    app.prepare(ctx_id=-1, det_thresh=float(cfg.detection.det_thresh), det_size=(640, 640))

    rgb = ImageOps.exif_transpose(im).convert("RGB")
    scale = 1.0
    if max(rgb.size) > 2000:
        scale = 2000 / max(rgb.size)
        rgb = rgb.resize((round(rgb.width * scale), round(rgb.height * scale)))
    bgr = np.asarray(rgb)[:, :, ::-1].copy()
    h, w = bgr.shape[:2]

    found = app.get(bgr)
    if not found:  # selfie case: face fills the frame, pad it (see models._detect)
        import cv2
        for pad in (0.5, 1.0):
            oy, ox = int(h * pad / 2), int(w * pad / 2)
            padded = cv2.copyMakeBorder(bgr, oy, oy, ox, ox, cv2.BORDER_CONSTANT, value=(114, 114, 114))
            found = app.get(padded)
            if found:
                for f in found:
                    f.bbox = f.bbox - np.array([ox, oy, ox, oy], dtype=f.bbox.dtype)
                    if f.kps is not None:
                        f.kps = f.kps - np.array([ox, oy], dtype=f.kps.dtype)
                break

    faces = []
    for f in sorted(found, key=lambda f: -(f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))[:limit]:
        x1, y1, x2, y2 = (float(v) / scale for v in f.bbox)
        bw, bh = x2 - x1, y2 - y1
        x1c, y1c = max(0, int(f.bbox[0])), max(0, int(f.bbox[1]))
        x2c, y2c = min(w, int(f.bbox[2])), min(h, int(f.bbox[3]))
        patch = bgr[y1c:y2c, x1c:x2c]
        entry = {
            "bbox": [round(x1), round(y1), round(x2), round(y2)],
            "size_pct_of_frame": round(100 * (bw * bh) / ((w / scale) * (h / scale)), 2),
            "det_score": round(float(f.det_score), 3),
        }
        if getattr(f, "age", None) is not None:
            entry["age_estimate"] = int(f.age)
        if getattr(f, "gender", None) is not None:
            entry["gender_estimate"] = "male" if int(f.gender) == 1 else "female"
        pose = getattr(f, "pose", None)
        if pose is not None:
            pitch, yaw, roll = (round(float(v), 1) for v in pose)
            entry["head_pose_deg"] = {"yaw": yaw, "pitch": pitch, "roll": roll}
            entry["facing"] = ("camera" if abs(yaw) < 15 else "three-quarter" if abs(yaw) < 45 else "profile")
        if patch.size:
            entry["blur_score"] = round(blur_score(patch), 1)
        entry["recognisable"] = bool(min(bw, bh) >= 80 and float(f.det_score) >= 0.6)
        faces.append(entry)
    return {"count": len(found), "faces": faces,
            "note": "age and gender are model guesses (InsightFace genderage), typically ±5 years; "
                    "'recognisable' means large and sharp enough for face matching"}


# ----------------------------------------------------------------------------- geocode

def reverse_geocode(lat: float, lon: float) -> dict:
    """OpenStreetMap Nominatim. One request, identified user agent, as their policy asks."""
    import httpx
    r = httpx.get("https://nominatim.openstreetmap.org/reverse",
                  params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18},
                  headers={"User-Agent": "faces-inspect/0.1 (https://github.com/oznakash/faces)"}, timeout=15)
    r.raise_for_status()
    j = r.json()
    return {"display_name": j.get("display_name"), "address": j.get("address", {}),
            "osm_type": j.get("osm_type"), "category": j.get("category"), "type": j.get("type")}


# ----------------------------------------------------------------------------- 7. describe (LLM)

DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "description": "One sentence: what the photo shows."},
        "setting": {"type": "string", "enum": ["indoor", "outdoor", "vehicle", "screen/scan", "unclear"]},
        "people": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "details": {"type": "array", "items": {"type": "object", "properties": {
                    "approx_age_range": {"type": "string"},
                    "visible_clothing_or_uniform": {"type": "string"},
                    "logos_badges_names_visible": {"type": "string"},
                    "activity": {"type": "string"},
                    "face_visible": {"type": "boolean"},
                }, "required": ["approx_age_range", "visible_clothing_or_uniform", "logos_badges_names_visible",
                                "activity", "face_visible"], "additionalProperties": False}},
            },
            "required": ["count", "details"], "additionalProperties": False,
        },
        "visible_text": {"type": "array", "items": {"type": "string"}, "description": "Every legible string, verbatim."},
        "location_clues": {"type": "array", "items": {"type": "object", "properties": {
            "clue": {"type": "string"}, "suggests": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        }, "required": ["clue", "suggests", "confidence"], "additionalProperties": False}},
        "time_clues": {"type": "array", "items": {"type": "object", "properties": {
            "clue": {"type": "string"}, "suggests": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        }, "required": ["clue", "suggests", "confidence"], "additionalProperties": False}},
        "objects_and_brands": {"type": "array", "items": {"type": "string"}},
        "sensitive_items": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"}, "why_it_matters": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        }, "required": ["item", "why_it_matters", "severity"], "additionalProperties": False},
            "description": "Things an adversary could use: addresses, plates, screens, documents, keys, "
                           "badges, reflections, children, home interior, routines."},
        "metadata_consistency": {"type": "string", "description": "Do the pixels agree with the metadata given?"},
    },
    "required": ["scene", "setting", "people", "visible_text", "location_clues", "time_clues", "objects_and_brands",
                 "sensitive_items", "metadata_consistency"],
    "additionalProperties": False,
}

DESCRIBE_SYSTEM = """You are a photo forensics analyst writing a privacy exposure report for the person who took or appears in the photo.

Report only what is observable in the pixels and what it concretely implies. Read all visible text verbatim. Name places only from evidence (signage, landmarks, plates, language, vegetation, architecture) and grade your confidence.

Do not guess ethnicity, nationality, religion, political views, sexual orientation, income, or health. Do not try to identify anyone by name. Describe people by what they wear and do, with a rough age range."""


def describe_layer(im, context: dict, model: str) -> dict:
    """One vision call. Image is downscaled so the request stays small."""
    import base64

    import anthropic
    from PIL import ImageOps

    rgb = ImageOps.exif_transpose(im).convert("RGB")
    if max(rgb.size) > 1568:
        rgb.thumbnail((1568, 1568))
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=88)
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    meta_lines = json.dumps(context, indent=1, default=str)
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        system=DESCRIBE_SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": DESCRIBE_SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": "Metadata already extracted from this file (may be absent or stripped):\n"
                                     f"{meta_lines}\n\nFill the report."},
        ]}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        return {"error": "model declined", "detail": getattr(resp.stop_details, "explanation", None)}
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        out = {"raw": text}
    out["_model"] = resp.model
    out["_usage"] = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return out


# ----------------------------------------------------------------------------- orchestration

def inspect(path: Path, *, faces: bool = True, geocode: bool = False, describe: bool = False,
            out_dir: Path | None = None, model: str = "claude-opus-5-5") -> dict:
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    data = path.read_bytes()
    im = Image.open(io.BytesIO(data))
    im.load()

    report: dict = {"file": file_layer(path, data, im)}
    exif = exif_layer(im)
    xmp_all, xmp_interesting = xmp_layer(data, im)
    iptc = iptc_layer(im)
    report["derived"] = derive(exif, xmp_all)
    report["exif"] = exif
    if xmp_interesting:
        report["xmp"] = xmp_interesting
    report["xmp_raw_count"] = len(xmp_all)
    if iptc:
        report["iptc"] = iptc
    thumb = thumbnail_layer(data, im, out_dir)
    if thumb:
        report["embedded_thumbnail"] = thumb
    report["filename"] = filename_layer(path.name, bool(exif), report["file"]["mime_sniffed"])

    place = report["derived"].get("place", {})
    if geocode and "latitude" in place:
        try:
            report["derived"]["place"]["reverse_geocode"] = reverse_geocode(place["latitude"], place["longitude"])
        except Exception as e:  # noqa: BLE001 — network nicety
            report["derived"]["place"]["reverse_geocode_error"] = str(e)

    if faces:
        try:
            report["faces"] = faces_layer(im)
        except Exception as e:  # noqa: BLE001 — model download or runtime failure should not lose the rest
            report["faces"] = {"error": str(e)}

    report["exposure"] = exposure_summary(report)

    if describe:
        ctx = {"derived": report["derived"], "xmp": report.get("xmp"), "iptc": report.get("iptc"),
               "filename": report["filename"], "faces": {k: v for k, v in report.get("faces", {}).items() if k != "note"}}
        try:
            report["describe"] = describe_layer(im, ctx, model)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            if "authentication" in msg.lower() or "api_key" in msg.lower():
                msg = "no Anthropic credentials — set ANTHROPIC_API_KEY (or run `ant auth login`) and pip install anthropic"
            report["describe"] = {"error": msg}

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    return report


def exposure_summary(r: dict) -> list[dict]:
    """What this file leaks, ranked. The part a reader acts on."""
    items = []
    d, x, i = r.get("derived", {}), r.get("xmp", {}), r.get("iptc", {})
    place = d.get("place", {})
    if "latitude" in place:
        err = place.get("horizontal_error_m")
        items.append({"severity": "high", "what": "Exact GPS position",
                      "detail": f"{place['latitude']}, {place['longitude']}" + (f" (±{err} m)" if err else "")
                                + (" — " + place["reverse_geocode"]["display_name"] if place.get("reverse_geocode", {}).get("display_name") else "")})
    if "xmp_gps" in place or x.get("xmp_gps_latitude"):
        items.append({"severity": "high", "what": "GPS position survives in XMP", "detail": "editor stripped EXIF GPS but not the XMP copy"})
    if d.get("time", {}).get("taken"):
        tz = d["time"].get("timezone_inferred")
        items.append({"severity": "medium", "what": "Capture time", "detail": d["time"]["taken"] + (f" ({d['time']['weekday']})" if d["time"].get("weekday") else "")
                                                                            + (f", home timezone {tz}" if tz else "")})
    serials = [d[k] for k in ("body_serial", "lens_serial") if d.get(k)] + [x[k] for k in ("body_serial", "lens_serial") if x.get(k)]
    if serials:
        items.append({"severity": "high", "what": "Hardware serial numbers", "detail": ", ".join(map(str, serials))
                      + " — links every photo from this camera to each other, and to a purchase record"})
    if d.get("image_unique_id"):
        items.append({"severity": "medium", "what": "Image unique ID", "detail": "device-generated ID links photos to the same device"})
    names = []
    for src, keys in ((d.get("owner_fields", {}), ("Artist", "Copyright", "XPAuthor", "CameraOwnerName")),
                      (x, ("creator", "rights", "owner_name", "creator_email", "creator_phone", "creator_url", "person_in_image",
                           "face_region_names", "ms_people_tags")),
                      (i, ("by_line", "copyright", "credit", "caption_writer"))):
        for k in keys:
            if src.get(k):
                names.append(f"{k}: {src[k]}")
    if names:
        items.append({"severity": "high", "what": "Names and contacts written into the file", "detail": "; ".join(map(str, names))})
    if x.get("face_region_names") or x.get("ms_people_tags") or x.get("person_in_image"):
        items.append({"severity": "high", "what": "People tagged by name", "detail": "face regions carry names from Picasa/Lightroom/Windows Photos"})
    if d.get("camera"):
        items.append({"severity": "low", "what": "Device", "detail": d["camera"] + (f", {d['lens']}" if d.get("lens") else "")})
    if d.get("software") or x.get("software"):
        items.append({"severity": "low", "what": "Software", "detail": str(d.get("software") or x.get("software"))})
    if x.get("edit_history_software"):
        items.append({"severity": "low", "what": "Edit history", "detail": str(x["edit_history_software"])})
    if d.get("host_computer"):
        items.append({"severity": "medium", "what": "Computer name", "detail": d["host_computer"]})
    if x.get("raw_file_name"):
        items.append({"severity": "low", "what": "Original RAW filename", "detail": x["raw_file_name"]})
    if x.get("cropped") or any(k in x for k in ("crop_top", "crop_left")):
        items.append({"severity": "medium", "what": "Crop recorded in XMP", "detail": "Lightroom keeps the crop rectangle; the full frame may be recoverable from the original"})
    th = r.get("embedded_thumbnail", {})
    if th.get("verdict") and not th["verdict"].startswith("matches"):
        items.append({"severity": "high", "what": "Embedded thumbnail disagrees with image", "detail": th["verdict"]
                      + (f" (saved to {th['saved_to']})" if th.get("saved_to") else "")})
    for k, label in (("city", "City"), ("country", "Country"), ("sublocation", "Location"), ("location_shown", "Location shown")):
        if x.get(k) or i.get(k):
            items.append({"severity": "medium", "what": label, "detail": str(x.get(k) or i.get(k))})
    kw = x.get("keywords") or i.get("keywords")
    if kw:
        items.append({"severity": "low", "what": "Keywords", "detail": ", ".join(kw) if isinstance(kw, list) else str(kw)})
    if x.get("digital_source_type"):
        items.append({"severity": "low", "what": "Digital source type", "detail": str(x["digital_source_type"])})
    f = r.get("faces", {})
    if f.get("count"):
        rec = sum(1 for z in f.get("faces", []) if z.get("recognisable"))
        items.append({"severity": "high" if rec else "medium", "what": f"{f['count']} face(s), {rec} recognisable",
                      "detail": "; ".join(f"~{z.get('age_estimate', '?')}y {z.get('gender_estimate', '')} {z.get('facing', '')}".strip()
                                          for z in f.get("faces", [])[:8])})
    hints = r.get("filename", {}).get("hints")
    if hints:
        items.append({"severity": "low", "what": "Filename pattern", "detail": "; ".join(hints)})
    png = r.get("file", {}).get("png_text_chunks")
    if png:
        items.append({"severity": "medium", "what": "PNG text chunks", "detail": "; ".join(f"{k}: {v}" for k, v in png.items())})
    comments = r.get("file", {}).get("comments")
    if comments:
        items.append({"severity": "medium", "what": "JPEG comment segment", "detail": " | ".join(comments)})
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(items, key=lambda z: order[z["severity"]])


# ----------------------------------------------------------------------------- text report

def render(r: dict) -> str:
    L = []
    f = r["file"]
    L.append(f"{f['name']}  —  {f['width']}×{f['height']} {f.get('pil_format')}  {f['bytes']:,} bytes  sha256 {f['sha256'][:16]}…")
    L.append("")
    L.append("WHAT IT GIVES AWAY")
    if not r["exposure"]:
        L.append("  nothing beyond the pixels — no metadata present")
    for e in r["exposure"]:
        L.append(f"  [{e['severity']:<6}] {e['what']}: {e['detail']}")
    d = r["derived"]
    if d:
        L.append("")
        L.append("DERIVED")
        for k, v in d.items():
            if isinstance(v, dict):
                L.append(f"  {k}:")
                for kk, vv in v.items():
                    if isinstance(vv, dict):
                        L.append(f"    {kk}: {json.dumps(vv, ensure_ascii=False, default=str)[:300]}")
                    else:
                        L.append(f"    {kk}: {vv}")
            else:
                L.append(f"  {k}: {v}")
    for key in ("xmp", "iptc", "embedded_thumbnail"):
        if r.get(key):
            L.append("")
            L.append(key.upper())
            for k, v in r[key].items():
                L.append(f"  {k}: {v}")
    fl = r.get("filename", {})
    if fl.get("hints") or fl.get("note"):
        L.append("")
        L.append("FILENAME / CONTAINER")
        for h in fl.get("hints", []):
            L.append(f"  {h}")
        if fl.get("note"):
            L.append(f"  {fl['note']}")
    segs = f.get("jpeg_segments")
    if segs:
        L.append("  segments: " + " → ".join(s.get("id") or s["name"] for s in segs))
        if f.get("jpeg_quality_estimate"):
            L.append(f"  jpeg quality ≈ {f['jpeg_quality_estimate']}, chroma {f.get('subsampling')}, {'progressive' if f.get('progressive') else 'baseline'}")
    fa = r.get("faces")
    if fa:
        L.append("")
        L.append(f"FACES  ({fa.get('count', 0)} detected)" if "error" not in fa else f"FACES  error: {fa['error']}")
        for z in fa.get("faces", []):
            pose = z.get("head_pose_deg", {})
            L.append(f"  bbox {z['bbox']}  {z['size_pct_of_frame']}% of frame  score {z['det_score']}  "
                     f"~{z.get('age_estimate', '?')}y {z.get('gender_estimate', '')}  {z.get('facing', '')}"
                     f"{' yaw ' + str(pose.get('yaw')) if pose else ''}  blur {z.get('blur_score')}  "
                     f"{'recognisable' if z.get('recognisable') else 'too small/soft to match'}")
    de = r.get("describe")
    if de:
        L.append("")
        L.append("WHAT A VISION MODEL READS OFF THE PIXELS" + (f"  ({de.get('_model')})" if de.get("_model") else ""))
        if "error" in de:
            L.append(f"  {de['error']}")
        else:
            L.append(f"  {de.get('scene')}  [{de.get('setting')}]")
            p = de.get("people", {})
            L.append(f"  people: {p.get('count')}")
            for z in p.get("details", []):
                L.append(f"    - {z.get('approx_age_range')}, {z.get('visible_clothing_or_uniform')}; {z.get('activity')}"
                         + (f"; visible: {z['logos_badges_names_visible']}" if z.get("logos_badges_names_visible") else ""))
            if de.get("visible_text"):
                L.append("  text: " + " | ".join(de["visible_text"]))
            for sect in ("location_clues", "time_clues"):
                if de.get(sect):
                    L.append(f"  {sect.replace('_', ' ')}:")
                    for c in de[sect]:
                        L.append(f"    - {c['clue']} → {c['suggests']} ({c['confidence']})")
            if de.get("objects_and_brands"):
                L.append("  objects/brands: " + ", ".join(de["objects_and_brands"]))
            if de.get("sensitive_items"):
                L.append("  sensitive:")
                for s in de["sensitive_items"]:
                    L.append(f"    [{s['severity']:<6}] {s['item']} — {s['why_it_matters']}")
            if de.get("metadata_consistency"):
                L.append(f"  consistency: {de['metadata_consistency']}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photo", type=Path)
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--no-faces", action="store_true", help="skip face detection (no model download)")
    ap.add_argument("--geocode", action="store_true", help="reverse-geocode GPS via OpenStreetMap (network)")
    ap.add_argument("--describe", action="store_true", help="send a downscaled copy to Claude for a pixel-level read (network, needs ANTHROPIC_API_KEY)")
    ap.add_argument("--model", default=os.environ.get("FACES_DESCRIBE_MODEL", "claude-opus-5-5"))
    ap.add_argument("--out", type=Path, help="directory for report.json and the extracted EXIF thumbnail")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(name)s %(message)s")
    if not a.verbose:
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger().setLevel(logging.ERROR)
        os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")
    if not a.photo.is_file():
        ap.error(f"{a.photo} is not a file")
    report = inspect(a.photo, faces=not a.no_faces, geocode=a.geocode, describe=a.describe, out_dir=a.out, model=a.model)
    if a.json:
        print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
