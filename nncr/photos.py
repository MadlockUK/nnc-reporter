"""Photo intake: EXIF GPS + timestamp, dedupe hash, thumbnails, HEIC support."""
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags, ImageOps

try:  # iPhone .HEIC support
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover
    HEIC_OK = False

SUPPORTED = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
_GPS_TAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ratio(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return v.numerator / v.denominator
        except Exception:
            return 0.0


def _dms_to_deg(dms, ref: str):
    if not dms or len(dms) < 3:
        return None
    deg = _ratio(dms[0]) + _ratio(dms[1]) / 60 + _ratio(dms[2]) / 3600
    if str(ref).upper().strip() in ("S", "W"):
        deg = -deg
    return round(deg, 7)


def read_metadata(path: Path) -> dict:
    """Return lat/lon/taken_at/bearing/dimensions. Missing values come back None."""
    out = {"lat": None, "lon": None, "taken_at": None, "bearing": None,
           "width": None, "height": None, "gps_error": None}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            exif = im.getexif()
            if not exif:
                out["gps_error"] = "no EXIF block in file"
                return out

            # DateTimeOriginal/Digitized live in the Exif sub-IFD; DateTime in IFD0.
            sub = {}
            try:
                sub = exif.get_ifd(ExifTags.IFD.Exif) or {}
            except Exception:
                pass
            for source, tag in ((sub, 36867), (sub, 36868), (exif, 306)):
                raw = source.get(tag)
                if raw:
                    try:
                        out["taken_at"] = datetime.strptime(
                            str(raw).strip(), "%Y:%m:%d %H:%M:%S").isoformat()
                        break
                    except ValueError:
                        continue

            gps = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
            if not gps:
                out["gps_error"] = "photo has no GPS tags (location services off?)"
                return out

            lat = _dms_to_deg(gps.get(_GPS_TAGS["GPSLatitude"]),
                              gps.get(_GPS_TAGS["GPSLatitudeRef"], "N"))
            lon = _dms_to_deg(gps.get(_GPS_TAGS["GPSLongitude"]),
                              gps.get(_GPS_TAGS["GPSLongitudeRef"], "E"))
            if lat is None or lon is None or (lat == 0 and lon == 0):
                out["gps_error"] = "GPS tags present but empty"
                return out
            out["lat"], out["lon"] = lat, lon

            bearing = gps.get(_GPS_TAGS.get("GPSImgDirection", -1))
            if bearing is not None:
                out["bearing"] = round(_ratio(bearing), 1)
    except Exception as e:  # unreadable/corrupt file
        out["gps_error"] = f"could not read image: {e}"
    return out


def in_north_northants(lat: float, lon: float) -> bool:
    """Rough bounding box for North Northamptonshire - catches obvious mistakes."""
    return 52.20 <= lat <= 52.72 and -1.05 <= lon <= -0.25


def store_photo(src: Path, sha: str, data_dir: Path) -> Path:
    dest = data_dir / "photos" / f"{sha[:16]}{src.suffix.lower()}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest


def make_thumb(src: Path, sha: str, data_dir: Path, size: int = 480) -> Path | None:
    dest = data_dir / "thumbs" / f"{sha[:16]}.jpg"
    if dest.exists():
        return dest
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((size, size))
            im.save(dest, "JPEG", quality=82)
        return dest
    except Exception:
        return None


# The council's limit is 15MB, but size costs time, not just acceptance: a 9MB
# photo takes long enough to upload that the form hides its Next button while it
# goes, and phone cameras produce 8000px files where 3000 is more than enough to
# show a fly-tip. 4MB keeps the evidence sharp and the upload quick.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_EDGE = 3000                   # plenty of detail for evidence


def to_jpeg_for_upload(src: Path, data_dir: Path,
                       max_bytes: int = MAX_UPLOAD_BYTES,
                       max_edge: int = MAX_UPLOAD_EDGE) -> Path:
    """A JPEG the council's uploader will accept.

    Phone photos are often 15-20MB, over the 15MB limit, and HEIC is rejected
    outright. Resize and re-encode until it fits - locally, in about a second.
    Rotation is applied first so stripping the metadata cannot leave it sideways.
    """
    src = Path(src)
    try:
        small_enough = src.stat().st_size <= max_bytes
    except OSError:
        small_enough = False
    if small_enough and src.suffix.lower() in (".jpg", ".jpeg"):
        return src

    dest = data_dir / "photos" / (src.stem + "_upload.jpg")
    try:                                  # reuse an earlier conversion
        if (dest.exists() and dest.stat().st_size <= max_bytes
                and dest.stat().st_mtime >= src.stat().st_mtime):
            return dest
    except OSError:
        pass

    with Image.open(src) as opened:
        im = ImageOps.exif_transpose(opened) or opened
        im = im.convert("RGB")
        for edge in (max_edge, 2400, 2000, 1600, 1200):
            work = im.copy()
            if max(work.size) > edge:
                work.thumbnail((edge, edge), Image.LANCZOS)
            for quality in (88, 82, 76, 70):
                work.save(dest, "JPEG", quality=quality, optimize=True,
                          progressive=True)
                if dest.stat().st_size <= max_bytes:
                    return dest
    return dest                            # best effort, still the smallest we made


def prepare_upload(stored: Path, data_dir: Path) -> tuple:
    """Get a photo ready to be sent, at intake rather than at submission time.

    A phone photo takes a second to shrink and a minute to upload, so doing it now
    - while you are already waiting for the photos to be read - makes the actual
    submission quick. The full-size original is kept alongside it.

    Returns (path that will be sent, {before, after, size} or {problem}). Never
    raises: a photo that cannot be shrunk is still worth queuing.
    """
    try:
        before = stored.stat().st_size
        ready = to_jpeg_for_upload(stored, data_dir)
        if ready == stored:
            return ready, {}                       # already small enough
        with Image.open(ready) as im:
            size = f"{im.width}x{im.height}"
        return ready, {"before": before, "after": ready.stat().st_size, "size": size}
    except Exception as e:
        return stored, {"problem": f"could not shrink this photo "
                                   f"({str(e).splitlines()[0]}) - it will be sent "
                                   f"full size, which may be slow"}


def describe_size(path: Path) -> str:
    try:
        mb = Path(path).stat().st_size / 1024 / 1024
        with Image.open(path) as im:
            return f"{im.size[0]}x{im.size[1]}, {mb:.1f}MB"
    except Exception:
        return "unknown size"
