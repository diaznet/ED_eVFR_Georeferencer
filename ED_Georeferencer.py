"""
ED eVFR Georeferencer

Builds georeferenced MBTiles of the German VFR aerodrome charts (DFS BasicVFR AIP)
for use in SDVFR Next.

Data sources:
  - Plates: scraped from https://aip.dfs.de/BasicVFR (high-resolution "print" PNGs).
  - Georeferences: the public AIP Browser DE open-data feed
    (https://mpmediasoft.de/OpenData/AIPBrowserDE/V2/VFR/aip-de-vfr-aux-model-metadata.json),
    which gives, per page, 2 or 3 ground control points in page-pixel coordinates,
    a crop box for the chart area, a rotation offset and the SHA-256 of the exact
    page image the points were measured on.

No chart content is shipped with this script; everything is downloaded at run time.

The georeferences are the work of AIP Browser DE by mpMediaSoft
(https://mpmediasoft.de/products/AIPBrowserDE/help/AIPBrowserDE.html). AIP Browser DE has no MBTiles
export, which SDVFR Next requires; this script only turns its georeferences and the DFS plates into MBTiles.
"""

import argparse
import base64
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import quote, urljoin

import numpy as np
import requests
from osgeo import gdal, osr
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

# --- Sources -----------------------------------------------------------------

GEOREF_URL = "https://mpmediasoft.de/OpenData/AIPBrowserDE/V2/VFR/aip-de-vfr-aux-model-metadata.json"
DFS_BASE_URL = "https://aip.dfs.de"
DFS_VFR_ROOT = DFS_BASE_URL + "/BasicVFR"

# aip.dfs.de answers 404 to non-browser user agents, and serves the reduced-size
# print image unless the Referer is the dated page the print link came from.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/76.0.3809.132 Safari/537.36")

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

# --- Output groups -------------------------------------------------------------
# Each group's max zoom matches the native resolution of its charts (a 1750 px wide A4 page):
#   1:200 000 .. 1:300 000 area charts  -> ~zoom 12-12.5
#   1:50 000  .. 1:150 000 approach     -> ~zoom 13-14.5
#   1:4 000   .. 1:20 000  aerodrome    -> ~zoom 16-18
# Tiling a chart beyond its native zoom only enlarges it (4x the tiles per extra level),
# so they go into separate files; map clients keep showing a layer past its max zoom.

AREA_SCALE_THRESHOLD = 200000
AD_SCALE_THRESHOLD = 25000

GROUPS = {
    "AREA": {"min_zoom": 8, "max_zoom": 13, "trim_frame": True,
             "match": lambda inv_scale: inv_scale >= AREA_SCALE_THRESHOLD},
    "VAC": {"min_zoom": 10, "max_zoom": 14, "trim_frame": True,
            "match": lambda inv_scale: AD_SCALE_THRESHOLD <= inv_scale < AREA_SCALE_THRESHOLD},
    "AD": {"min_zoom": 13, "max_zoom": 16, "trim_frame": False,
           "match": lambda inv_scale: inv_scale < AD_SCALE_THRESHOLD},
}

# --- Web Mercator tile grid ----------------------------------------------------

ORIGIN_SHIFT = 20037508.342789244
TILE_SIZE = 256


def _resolution(zoom):
    return 2 * ORIGIN_SHIFT / (TILE_SIZE * 2 ** zoom)


# --- AIRAC -----------------------------------------------------------------------

AIRAC_EPOCH = date(2024, 1, 25)  # cycle 2401
AIRAC_CYCLE_DAYS = 28


def get_airac_date(reference_date):
    """Effective date of the AIRAC cycle containing reference_date."""
    cycles = (reference_date - AIRAC_EPOCH).days // AIRAC_CYCLE_DAYS
    return AIRAC_EPOCH + timedelta(days=cycles * AIRAC_CYCLE_DAYS)


def get_airac_cycle_id(airac_date):
    """AIRAC identifier in YYNN format, e.g. 2026-10-01 -> '2610'."""
    year_start = date(airac_date.year, 1, 1)
    first = get_airac_date(year_start)
    if first < year_start:
        first += timedelta(days=AIRAC_CYCLE_DAYS)
    number = (airac_date - first).days // AIRAC_CYCLE_DAYS + 1
    return f"{airac_date:%y}{number:02d}"


def parse_dfs_edition(folder_name):
    """'2026OCT20' -> date(2026, 10, 20)."""
    m = re.fullmatch(r"(\d{4})([A-Z]{3})(\d{2})", folder_name)
    if not m or m.group(2) not in MONTHS:
        return None
    return date(int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


# --- Download ----------------------------------------------------------------------

class BlockedError(RuntimeError):
    """aip.dfs.de answered 403: the server is refusing this client, continuing would only make it worse."""


# Pause before every request to aip.dfs.de; its IIS front end blocks clients that request too fast.
REQUEST_DELAY = 1.0


def _session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(session, url, referer, retries=3):
    for attempt in range(retries):
        time.sleep(REQUEST_DELAY)
        try:
            r = session.get(url, headers={"Referer": referer}, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                raise BlockedError(f"HTTP 403 for {url}")
            if r.status_code == 404:
                raise RuntimeError(f"HTTP 404 for {url}")
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"{e} for {url}") from e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Giving up on {url}")


def load_georefs(cache_dir):
    """Downloads the georeference feed (falls back to the cached copy when offline)."""
    path = os.path.join(cache_dir, "aip-de-vfr-aux-model-metadata.json")
    try:
        r = requests.get(GEOREF_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        print(f"Downloaded georeferences: {GEOREF_URL}")
    except requests.RequestException as e:
        if not os.path.exists(path):
            raise
        print(f"WARNING: could not refresh georeferences ({e}), using cached copy.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["pageGeoInfos"]


def download_plate(session, perm_page_id):
    """
    Follows the same path as AIP Browser DE:
      pages/<id>.html  --meta refresh-->  <EDITION>/pages/<hash>.html  --myURL-->  print/<myURL>
    Returns (image_bytes, kind, edition_folder) where kind is 'png' or 'pdf'.
    """
    perma_url = f"{DFS_VFR_ROOT}/pages/{perm_page_id[1:]}.html"
    html = _get(session, perma_url, DFS_BASE_URL).text

    m = re.search(r'http-equiv="Refresh"\s+content="0;\s*url=([^"]+)"', html, re.I)
    if not m:
        raise RuntimeError(f"No redirect found on {perma_url}")
    page_url = urljoin(perma_url, m.group(1).replace(" ", "%20"))
    edition = page_url.split("/BasicVFR/", 1)[1].split("/", 1)[0]

    html = _get(session, page_url, DFS_BASE_URL).text
    m = re.search(r'myURL\s*=\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError(f"No print link found on {page_url}")
    print_url = f"{DFS_VFR_ROOT}/print/{quote(m.group(1), safe='/')}"

    r = _get(session, print_url, page_url)
    if r.headers.get("Content-Type", "").startswith("application/pdf"):
        return r.content, "pdf", edition
    m = re.search(r'<img[^>]+src="data:image/png;base64,([A-Za-z0-9+/=\s]+)"', r.text)
    if not m:
        raise RuntimeError(f"No page image found on {print_url}")
    return base64.b64decode(m.group(1)), "png", edition


def _plate_path(cache_dir, perm_page_id, ext):
    """Cached plate file: plates/<id>.png or .pdf (the raw DFS payload), plus plates/<id>.json metadata."""
    return os.path.join(cache_dir, "plates", f"{perm_page_id}.{ext}")


def load_cached_plate(georef, cache_dir):
    """Returns the cached plate's metadata if its image matches the georeference checksum, else None."""
    pid = georef["info"]["permPageId"]
    meta_path = _plate_path(cache_dir, pid, "json")
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    legacy = _plate_path(cache_dir, pid, "bin")  # caches from earlier versions of this script
    if os.path.exists(legacy) and not os.path.exists(_plate_path(cache_dir, pid, meta["kind"])):
        os.replace(legacy, _plate_path(cache_dir, pid, meta["kind"]))
    if meta["sha256"] != georef["info"]["checksumHexValue"] or not os.path.exists(_plate_path(cache_dir, pid, meta["kind"])):
        return None
    meta["cached"] = True
    return meta


def fetch_plate(session, georef, cache_dir, refresh=False):
    """Returns a dict describing the cached plate, downloading it if needed."""
    if not refresh:
        meta = load_cached_plate(georef, cache_dir)
        if meta:
            return meta

    pid = georef["info"]["permPageId"]
    data, kind, edition = download_plate(session, pid)
    meta = {"id": pid, "name": georef["name"], "kind": kind, "edition": edition,
            "sha256": hashlib.sha256(data).hexdigest(), "cached": False}
    with open(_plate_path(cache_dir, pid, kind), "wb") as f:
        f.write(data)
    with open(_plate_path(cache_dir, pid, "json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


def download_all(georefs, cache_dir, workers, refresh=False):
    os.makedirs(os.path.join(cache_dir, "plates"), exist_ok=True)
    session = _session()
    results = {}
    print(f"\n--- Step 1: Fetching {len(georefs)} plates from {DFS_VFR_ROOT} ---")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_plate, session, g, cache_dir, refresh): g for g in georefs}
        for i, fut in enumerate(as_completed(futures), 1):
            g = futures[fut]
            pid = g["info"]["permPageId"]
            try:
                meta = fut.result()
                results[pid] = meta
                state = "cached" if meta["cached"] else "downloaded"
                if meta["sha256"] != g["info"]["checksumHexValue"]:
                    state += ", CHECKSUM MISMATCH (page changed since it was georeferenced)"
                print(f"[{i}/{len(georefs)}] {pid} {g['name']}: {state}")
            except BlockedError as e:
                for f in futures:
                    f.cancel()
                print(f"[{i}/{len(georefs)}] {pid} {g['name']}: {e}")
                print("\naip.dfs.de is refusing requests (403). Stopping downloads; wait a while before retrying.\n"
                      "Plates already in the cache are kept, a later run only fetches the missing ones.")
                for other in georefs:
                    other_pid = other["info"]["permPageId"]
                    if other_pid not in results and (meta := load_cached_plate(other, cache_dir)):
                        results[other_pid] = meta
                break
            except Exception as e:
                print(f"[{i}/{len(georefs)}] {pid} {g['name']}: ERROR {e}")
    return results


# --- Georeferencing --------------------------------------------------------------------

def load_page_image(path, kind, info):
    if kind == "png":
        return Image.open(path).convert("RGBA")
    try:
        import fitz  # PyMuPDF, only needed if DFS ever serves a PDF page
    except ImportError:
        raise RuntimeError("page is a PDF; install PyMuPDF to render it")
    with fitz.open(path) as doc:
        page = doc[0]
        rotated = info.get("rotationOffset", 0) in (90, 270)
        target_w = info["pageImgHeight"] if rotated else info["pageImgWidth"]
        zoom = target_w / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")


# --- Chart frame removal --------------------------------------------------------------
# The georeference crop box includes the chart frame, which has the same geometry on every plate:
# a thick outer border (3-4 px), a 46 px band with the lat/lon labels and ticks, then a thin
# neatline (inner border) around the map. Where the frame starts inside the crop box varies:
# usually right at the edge, but some crop boxes also contain correction notes, title bands or
# boxes sticking out of the frame.

FRAME_WIDTH = 47  # end of the outer border -> inside of the neatline (measured 46-48 px on all plates)


def _runs(indices):
    out = []
    for i in indices:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def _side_inset(fill):
    """
    fill: per-line coverage by frame colour, ordered from the crop edge inwards.
    Finds the outer border (first long line at least 3 px thick), then the neatline FRAME_WIDTH px
    further in. The neatline is thin and often interrupted by symbols or text boxes, so if it isn't
    found the known frame width is used.
    A few crop boxes clip the outer border to a thin line at the edge, or have no label band on one
    side (the map runs up to a thin border, or to the crop edge itself).
    """
    lines = _runs(np.flatnonzero(fill >= 0.6))
    outer = next((r for r in lines if r[1] - r[0] >= 2), None)
    if outer is not None:
        expected = outer[1] + FRAME_WIDTH
        neat = [r for r in _runs(np.flatnonzero(fill >= 0.3)) if abs(r[1] + 1 - expected) <= 3]
        return neat[0][1] + 1 if neat else expected
    edge = next((r for r in lines if r[0] < 10), None)
    edge_end = edge[1] if edge else -1  # outer border clipped to a thin line, or cropped away
    neat = [r for r in lines if edge_end + FRAME_WIDTH - 9 <= r[1] + 1 <= edge_end + FRAME_WIDTH + 3]
    return neat[0][1] + 1 if neat else edge_end + 1


def find_frame_insets(arr, margin=0.15):
    """Returns (left, top, right, bottom) px to trim from a cropped chart to remove its frame."""
    rgb = arr[:, :, :3].astype(np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    frame = (b - r > 40) & (b - g > 40) & (r < 140)  # the dark blue frame lines
    h, w = frame.shape
    my, mx = int(h * margin), int(w * margin)
    # Profiles over the middle of the other axis, so the frame corners and labels don't count.
    rows, cols = frame[:, mx:w - mx].mean(axis=1), frame[my:h - my, :].mean(axis=0)
    return (_side_inset(cols[:mx]), _side_inset(rows[:my]),
            _side_inset(cols[::-1][:mx]), _side_inset(rows[::-1][:my]))


def geotransform_from_refpoints(ref_points, crop_min_x, crop_min_y):
    """
    Pixel (cropped image) -> lon/lat as a GDAL geotransform, reproducing AIP Browser DE:
      2 points: translate + independent x/y scale (lon depends only on x, lat only on y)
      3 points: general affine transformation
    """
    pts = [(p["x"] - crop_min_x, p["y"] - crop_min_y, p["longitude"], p["latitude"]) for p in ref_points]
    if len(pts) == 2:
        (x1, y1, lon1, lat1), (x2, y2, lon2, lat2) = pts
        sx = (lon2 - lon1) / (x2 - x1)
        sy = (lat2 - lat1) / (y2 - y1)
        return (lon1 - sx * x1, sx, 0.0, lat1 - sy * y1, 0.0, sy)
    if len(pts) == 3:
        a = np.array([[x, y, 1.0] for x, y, _, _ in pts])
        lon_c = np.linalg.solve(a, np.array([p[2] for p in pts]))
        lat_c = np.linalg.solve(a, np.array([p[3] for p in pts]))
        return (lon_c[2], lon_c[0], lon_c[1], lat_c[2], lat_c[0], lat_c[1])
    raise ValueError(f"unsupported number of reference points: {len(pts)}")


def georeference_plate(georef, meta, cache_dir, zoom, threads=1, trim_frame=False):
    """Returns a GDAL MEM dataset in EPSG:3857, pixel-aligned to the tile grid at `zoom`."""
    info = georef["info"]
    path = _plate_path(cache_dir, info["permPageId"], meta["kind"])
    img = load_page_image(path, meta["kind"], info)

    rotation = info.get("rotationOffset", 0)
    if rotation:
        # AIP Browser DE rotates clockwise (Java2D, y axis down) before cropping.
        img = img.rotate(-rotation, expand=True)
    if img.size != (info["pageImgWidth"], info["pageImgHeight"]):
        raise RuntimeError(f"page image is {img.size}, georeference expects "
                           f"{(info['pageImgWidth'], info['pageImgHeight'])}")

    min_x, min_y, max_x, max_y = info["cropMinX"], info["cropMinY"], info["cropMaxX"], info["cropMaxY"]
    if trim_frame:
        left, top, right, bottom = find_frame_insets(np.asarray(img.crop((min_x, min_y, max_x, max_y))))
        min_x, min_y, max_x, max_y = min_x + left, min_y + top, max_x - right, max_y - bottom
    arr = np.asarray(img.crop((min_x, min_y, max_x, max_y)))
    h, w = arr.shape[:2]

    src = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    for b in range(4):
        src.GetRasterBand(b + 1).WriteArray(arr[:, :, b])
    src.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    src.SetGeoTransform(geotransform_from_refpoints(georef["refPoints"], min_x, min_y))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src.SetProjection(srs.ExportToWkt())

    res = _resolution(zoom)
    return gdal.Warp("", src, format="MEM", dstSRS="EPSG:3857", xRes=res, yRes=res,
                     targetAlignedPixels=True, resampleAlg=gdal.GRA_Cubic,
                     multithread=threads > 1, warpOptions=[f"NUM_THREADS={threads}"])


# --- MBTiles writer -----------------------------------------------------------------------

def _decode(data):
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _encode(img, colors=0, compress_level=1):
    """
    PNG-encodes a tile. Fully opaque tiles are stored as RGB. With colors > 0 the tile is reduced to
    a palette: the charts use few flat colours, so 256 colours look identical at ~1/5 of the size.
    Pillow releases the GIL while compressing, so this scales across threads.
    """
    if img.getextrema()[3][0] == 255:
        img = img.convert("RGB")
    if colors:
        img = img.quantize(colors, method=Image.Quantize.FASTOCTREE)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=compress_level)
    return buf.getvalue()


def _compose_tile(job):
    """Draws a new tile over an already stored one (if any). Runs in a worker thread."""
    z, x, y_tms, tile, existing = job
    overlay = Image.fromarray(tile, "RGBA")
    return z, x, y_tms, _encode(Image.alpha_composite(_decode(existing), overlay) if existing else overlay)


def _finalize_tile(job):
    """Final encoding of a lossless working tile. Runs in a worker thread."""
    rowid, data, colors = job
    return _encode(_decode(data), colors, compress_level=6), rowid


def _overview_tile(job):
    """Downsamples up to 4 child tiles into their parent. Runs in a worker thread."""
    z, px, py, children = job
    canvas = Image.new("RGBA", (TILE_SIZE * 2, TILE_SIZE * 2), (0, 0, 0, 0))
    for (dx, dy), data in children.items():
        # TMS rows grow northwards: the upper child has the higher row number.
        canvas.paste(_decode(data), (dx * TILE_SIZE, (1 - dy) * TILE_SIZE))
    return z, px, py, _encode(canvas.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS))


class MBTilesWriter:
    """
    Tiles are kept lossless (fast PNG) while charts are composited and overviews are built,
    then re-encoded once in close() with the requested palette size.
    """

    def __init__(self, path, name, description, min_zoom, max_zoom, threads, colors):
        if os.path.exists(path):
            os.remove(path)
        self.path = path
        self.min_zoom, self.max_zoom = min_zoom, max_zoom
        self.colors = colors
        self.pool = ThreadPoolExecutor(max_workers=threads)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE metadata (name TEXT, value TEXT);
            CREATE UNIQUE INDEX metadata_name ON metadata (name);
            CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
            CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
        """)
        self.meta = {"name": name, "description": description, "type": "overlay", "version": "1.0",
                     "format": "png", "minzoom": str(min_zoom), "maxzoom": str(max_zoom)}

    def _get(self, z, x, y_tms):
        row = self.db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                              (z, x, y_tms)).fetchone()
        return row[0] if row else None

    def _put_all(self, worker, jobs):
        self.db.executemany("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)", self.pool.map(worker, jobs))
        self.db.commit()

    def add_raster(self, ds):
        """Cuts a tile-aligned EPSG:3857 RGBA dataset into max_zoom tiles, compositing over existing ones."""
        z = self.max_zoom
        res = _resolution(z)
        gt = ds.GetGeoTransform()
        px0 = round((gt[0] + ORIGIN_SHIFT) / res)
        py0 = round((ORIGIN_SHIFT - gt[3]) / res)
        arr = np.dstack([ds.GetRasterBand(b + 1).ReadAsArray() for b in range(4)])
        h, w = arr.shape[:2]
        n = 2 ** z
        jobs = []
        for tx in range(px0 // TILE_SIZE, (px0 + w - 1) // TILE_SIZE + 1):
            for ty in range(py0 // TILE_SIZE, (py0 + h - 1) // TILE_SIZE + 1):
                tile = np.zeros((TILE_SIZE, TILE_SIZE, 4), np.uint8)
                x0, y0 = tx * TILE_SIZE - px0, ty * TILE_SIZE - py0
                sx0, sy0 = max(x0, 0), max(y0, 0)
                sx1, sy1 = min(x0 + TILE_SIZE, w), min(y0 + TILE_SIZE, h)
                tile[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = arr[sy0:sy1, sx0:sx1]
                if not tile[:, :, 3].any():
                    continue
                y_tms = n - 1 - ty
                jobs.append((z, tx, y_tms, tile, self._get(z, tx, y_tms)))
        self._put_all(_compose_tile, jobs)

    def build_overviews(self, batch_size=1024):
        for z in range(self.max_zoom - 1, self.min_zoom - 1, -1):
            parents = self.db.execute(
                "SELECT DISTINCT tile_column / 2, tile_row / 2 FROM tiles WHERE zoom_level=?", (z + 1,)).fetchall()
            for i in range(0, len(parents), batch_size):
                jobs = []
                for px, py in parents[i:i + batch_size]:
                    children = {}
                    for dx in (0, 1):
                        for dy in (0, 1):
                            data = self._get(z + 1, px * 2 + dx, py * 2 + dy)
                            if data:
                                children[(dx, dy)] = data
                    jobs.append((z, px, py, children))
                self._put_all(_overview_tile, jobs)
            print(f"  zoom {z}: {len(parents)} tiles")

    def finalize_tiles(self, batch_size=2048):
        total = self.db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        last = 0
        while True:
            rows = self.db.execute("SELECT rowid, tile_data FROM tiles WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                   (last, batch_size)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            jobs = [(rowid, data, self.colors) for rowid, data in rows]
            self.db.executemany("UPDATE tiles SET tile_data=? WHERE rowid=?", self.pool.map(_finalize_tile, jobs))
            self.db.commit()
        print(f"  encoded {total} tiles ({f'{self.colors} colour palette' if self.colors else 'true colour'})")

    def close(self, bounds):
        self.finalize_tiles()
        west, south, east, north = bounds
        self.meta["bounds"] = f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"
        self.meta["center"] = f"{(west + east) / 2:.6f},{(south + north) / 2:.6f},{self.min_zoom}"
        self.db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", self.meta.items())
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()
        self.pool.shutdown()


def _wgs84_bounds(ds):
    gt = ds.GetGeoTransform()
    minx, maxy = gt[0], gt[3]
    maxx, miny = minx + gt[1] * ds.RasterXSize, maxy + gt[5] * ds.RasterYSize
    to_lon = lambda x: math.degrees(x / 6378137.0)
    to_lat = lambda y: math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
    return to_lon(minx), to_lat(miny), to_lon(maxx), to_lat(maxy)


def create_mbtiles(georefs, plates, cache_dir, output_dir, cycle_id, groups, allow_mismatch, keep_geotiff,
                   threads, colors):
    mbtiles_dir = os.path.join(output_dir, "MBTiles")
    os.makedirs(mbtiles_dir, exist_ok=True)
    if keep_geotiff:
        os.makedirs(os.path.join(output_dir, "Geotiff_Charts"), exist_ok=True)

    usable = []
    for g in georefs:
        meta = plates.get(g["info"]["permPageId"])
        if not meta:
            continue
        if meta["sha256"] != g["info"]["checksumHexValue"] and not allow_mismatch:
            print(f"SKIPPING {g['name']}: page changed since it was georeferenced (checksum mismatch).")
            continue
        usable.append((g, meta))

    created = []
    for group_name, cfg in groups.items():
        members = [(g, m) for g, m in usable if cfg["match"](g["info"]["invScale"])]
        if not members:
            continue
        # Coarse charts first so that more detailed charts end up on top where they overlap.
        members.sort(key=lambda gm: -gm[0]["info"]["invScale"])

        filename = f"ED_{group_name}_{cycle_id}.mbtiles"
        path = os.path.join(mbtiles_dir, filename)
        print(f"\n--- Creating {filename} from {len(members)} charts (zoom {cfg['min_zoom']}-{cfg['max_zoom']}) ---")
        writer = MBTilesWriter(path, f"ED {group_name} {cycle_id}",
                               f"German VFR {group_name} charts, AIRAC {cycle_id}",
                               cfg["min_zoom"], cfg["max_zoom"], threads, colors)
        bounds = [180.0, 90.0, -180.0, -90.0]
        for i, (g, meta) in enumerate(members, 1):
            try:
                ds = georeference_plate(g, meta, cache_dir, cfg["max_zoom"], threads, cfg["trim_frame"])
                writer.add_raster(ds)
                b = _wgs84_bounds(ds)
                bounds = [min(bounds[0], b[0]), min(bounds[1], b[1]), max(bounds[2], b[2]), max(bounds[3], b[3])]
                if keep_geotiff:
                    tif = os.path.join(output_dir, "Geotiff_Charts", f"{g['info']['permPageId']}.tif")
                    gdal.Translate(tif, ds, creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
                ds = None
                print(f"[{i}/{len(members)}] {g['name']}")
            except Exception as e:
                print(f"[{i}/{len(members)}] {g['name']}: ERROR {e}")
        print("  Building lower zoom levels...")
        writer.build_overviews()
        writer.close(bounds)
        created.append(path)
        print(f"  SUCCESS: {path}")
    return created


# --- Main ---------------------------------------------------------------------------------

def _zoom_range(text):
    m = re.fullmatch(r"(\d+)-(\d+)", text)
    if not m or int(m.group(1)) > int(m.group(2)):
        raise argparse.ArgumentTypeError("expected MIN-MAX, e.g. 10-14")
    return int(m.group(1)), int(m.group(2))


def main():
    parser = argparse.ArgumentParser(description="Create SDVFR Next MBTiles from the German VFR AIP (DFS).")
    parser.add_argument("--output-path", default=".", help="Output directory (MBTiles/ is created inside).")
    parser.add_argument("--cache-dir", default="./cache", help="Where downloaded plates and georeferences are kept.")
    parser.add_argument("--filter", nargs="+", help="Only process charts whose name contains one of these strings (e.g. EDKA EDDF).")
    parser.add_argument("--groups", nargs="+", choices=list(GROUPS), default=list(GROUPS), help="Which MBTiles to build.")
    parser.add_argument("--area-zoom", type=_zoom_range, help=f"Zoom range for 1:200 000+ area charts (default {GROUPS['AREA']['min_zoom']}-{GROUPS['AREA']['max_zoom']}).")
    parser.add_argument("--vac-zoom", type=_zoom_range, help=f"Zoom range for approach charts (default {GROUPS['VAC']['min_zoom']}-{GROUPS['VAC']['max_zoom']}).")
    parser.add_argument("--ad-zoom", type=_zoom_range, help=f"Zoom range for aerodrome charts (default {GROUPS['AD']['min_zoom']}-{GROUPS['AD']['max_zoom']}).")
    parser.add_argument("--threads", type=int, default=os.cpu_count(),
                        help=f"Threads for warping and tile encoding (default: all {os.cpu_count()} cores).")
    parser.add_argument("--colors", type=int, default=256,
                        help="Palette size per tile, 2-256 (default 256, ~5x smaller than true colour). 0 = true colour PNG.")
    parser.add_argument("--airac", help="Force the AIRAC cycle used in file names (YYNN, e.g. 2611). Default: derived from the DFS edition.")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel downloads (default 1; aip.dfs.de blocks clients that request too fast).")
    parser.add_argument("--refresh", action="store_true", help="Re-download plates even if a matching cached copy exists.")
    parser.add_argument("--allow-checksum-mismatch", action="store_true",
                        help="Also tile pages that changed since they were georeferenced (may be misaligned).")
    parser.add_argument("--keep-geotiff", action="store_true", help="Also write per-chart GeoTIFFs (EPSG:3857) for inspection in QGIS.")
    parser.add_argument("--download-only", action="store_true", help="Only fetch plates, do not build MBTiles.")
    parser.add_argument("--keep-frame", action="store_true",
                        help="Keep the lat/lon frame around AREA and VAC charts (removed by default).")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be >= 1")
    if args.colors != 0 and not 2 <= args.colors <= 256:
        parser.error("--colors must be 0 or between 2 and 256")

    gdal.UseExceptions()
    os.makedirs(args.cache_dir, exist_ok=True)

    georefs = load_georefs(args.cache_dir)
    if args.filter:
        georefs = [g for g in georefs if any(f.lower() in g["name"].lower() for f in args.filter)]
    if not georefs:
        print("No charts match the filter."); return 1

    plates = download_all(georefs, args.cache_dir, args.workers, args.refresh)
    if not plates:
        print("No plates could be downloaded."); return 1

    # AIRAC cycle: from the DFS edition the plates were actually served from.
    if args.airac:
        if not re.fullmatch(r"\d{4}", args.airac):
            parser.error("--airac must be YYNN, e.g. 2611")
        cycle_id = args.airac
    else:
        editions = Counter(p["edition"] for p in plates.values())
        edition_date = parse_dfs_edition(editions.most_common(1)[0][0]) or date.today()
        cycle_id = get_airac_cycle_id(get_airac_date(edition_date))
        print(f"\nDFS edition {editions.most_common(1)[0][0]} -> AIRAC {cycle_id}")

    mismatches = sum(1 for g in georefs if g["info"]["permPageId"] in plates
                     and plates[g["info"]["permPageId"]]["sha256"] != g["info"]["checksumHexValue"])
    print(f"Plates: {len(plates)}/{len(georefs)} available, {mismatches} changed since georeferencing.")
    if args.download_only:
        return 0

    zoom_overrides = {"AREA": args.area_zoom, "VAC": args.vac_zoom, "AD": args.ad_zoom}
    groups = {}
    for name in args.groups:
        cfg = dict(GROUPS[name])
        if zoom_overrides[name]:
            cfg["min_zoom"], cfg["max_zoom"] = zoom_overrides[name]
        if args.keep_frame:
            cfg["trim_frame"] = False
        groups[name] = cfg

    created = create_mbtiles(georefs, plates, args.cache_dir, args.output_path, cycle_id, groups,
                             args.allow_checksum_mismatch, args.keep_geotiff, args.threads, args.colors)
    print("\nCreated:" if created else "\nNothing created.")
    for p in created:
        print(f"  {p}")
    return 0 if created else 1


if __name__ == "__main__":
    sys.exit(main())
