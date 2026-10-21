# ED eVFR Georeferencer

Builds georeferenced MBTiles of the German VFR aerodrome charts (DFS BasicVFR AIP) for SDVFR Next.

The repository contains **only the script**. Charts and georeferences are downloaded at run time.

## Credits: AIP Browser DE

The georeferences used by this script are **not** produced by this project. They come from
[AIP Browser DE](https://mpmediasoft.de/products/AIPBrowserDE/help/AIPBrowserDE.html) by mpMediaSoft
([public repository](https://github.com/mipastgt/AIPBrowserDE-Public)), whose author maintains them by hand and
publishes them as open data. All credit for the georeferencing work goes to them.

AIP Browser DE already displays these charts georeferenced, but it has **no MBTiles export**, which is the format
SDVFR Next needs for chart overlays. This project exists only to fill that gap: it combines the AIP Browser DE
georeferences with the plates from the DFS AIP and writes MBTiles. If you just want to browse the German AIP,
use AIP Browser DE.

This project is not affiliated with mpMediaSoft or DFS.

## How it works

1. **Georeferences**: downloads the AIP Browser DE open-data feed (see [Credits](#credits-aip-browser-de))
   `https://mpmediasoft.de/OpenData/AIPBrowserDE/V2/VFR/aip-de-vfr-aux-model-metadata.json`.
   For every chart page it provides 2 or 3 control points (page pixel → lon/lat), the chart crop box,
   a rotation offset and the SHA-256 of the exact page image the points were measured on.
2. **Plates**: for each page, follows the same path as AIP Browser DE on `aip.dfs.de`:
   `BasicVFR/pages/<id>.html` → meta-refresh to the current edition (e.g. `2026OCT20/pages/<hash>.html`)
   → `myURL` → `BasicVFR/print/<myURL>` → embedded base64 PNG (1750×2480 for a portrait A4 page).
   A browser User-Agent and the edition page as Referer are required, otherwise DFS returns 404 or a reduced image.
3. **Verification**: the downloaded PNG's SHA-256 must equal the georeference checksum. Pages that changed since they
   were georeferenced are skipped (override with `--allow-checksum-mismatch`).
4. **Georeferencing**: rotate (clockwise by `rotationOffset`), crop, then apply the same transform as AIP Browser DE:
   2 points → translate + independent x/y scale; 3 points → full affine. For AREA and VAC charts the lat/lon frame
   is trimmed at the map's inner border, so neighbouring charts join cleanly. The frame is identical on every
   plate (thick outer border, then the inner border 47 px further in); the script finds the outer border on each
   side because some crop boxes also contain notes or map parts sticking out of the frame. Warped to EPSG:3857, aligned to the tile grid.
5. **MBTiles**: tiles are cut at max zoom, composited (detailed charts drawn over coarser ones), and lower zooms are
   built by downsampling. Tiles stay lossless during the build and are encoded once at the end as 256-colour palette
   PNGs (the charts use few flat colours, so this looks identical at ~1/5 of the size).

## Output

| File | Content | Default zoom |
|------|---------|--------------|
| `MBTiles/ED_AREA_<AIRAC>.mbtiles` | Area charts, 1:200 000 – 1:300 000 | 8–13 |
| `MBTiles/ED_VAC_<AIRAC>.mbtiles` | Visual approach charts, 1:50 000 – 1:150 000 | 10–14 |
| `MBTiles/ED_AD_<AIRAC>.mbtiles`  | Aerodrome ground charts, 1:4 000 – 1:20 000 | 13–16 |

Each max zoom is close to the native resolution of the charts in that file. Tiling a chart beyond its native
zoom only enlarges it (4× the tiles per extra level); map clients keep displaying a layer past its max zoom.

`<AIRAC>` is set automatically (YYNN, e.g. `2610`). It is the AIRAC cycle that contains the DFS edition date
the plates were actually served from (the `2026OCT20` folder → cycle 2610). Use `--airac 2611` to force it.

## Usage

```bash
pip install -r requirements.txt

# Everything
python ED_Georeferencer.py --output-path ./output

# A few aerodromes, keeping per-chart GeoTIFFs for checking in QGIS
python ED_Georeferencer.py --filter EDKA EDDF --keep-geotiff

# Custom zoom ranges / only VAC, 8 threads, true colour tiles
python ED_Georeferencer.py --groups VAC --vac-zoom 9-14 --threads 8 --colors 0
```

| Option | Description |
|--------|-------------|
| `--output-path` | Output directory (default `.`) |
| `--cache-dir` | Downloaded plates + georeference feed (default `./cache`). Plates are stored as `plates/<pageId>.png` (or `.pdf`) with a `<pageId>.json` sidecar (name, DFS edition, SHA-256). Plates whose checksum still matches are not downloaded again. |
| `--filter` | Only charts whose name contains one of the strings |
| `--groups` | `AREA`, `VAC`, `AD` (default all) |
| `--area-zoom`, `--vac-zoom`, `--ad-zoom` | Zoom range as `MIN-MAX` |
| `--threads` | Threads for warping and tile encoding (default: all cores) |
| `--colors` | Palette size per tile, 2–256 (default 256). `0` = true colour PNG (~5× larger) |
| `--airac` | Force the cycle used in file names |
| `--workers` | Parallel downloads (default 1) |
| `--refresh` | Re-download all plates |
| `--allow-checksum-mismatch` | Also tile pages changed since georeferencing (may be misaligned) |
| `--keep-geotiff` | Write `Geotiff_Charts/<pageId>.tif` |
| `--download-only` | Fetch plates only |
| `--keep-frame` | Keep the lat/lon frame around AREA and VAC charts |

## Notes

- The georeference feed is maintained by the AIP Browser DE author and may lag behind a new DFS edition. Changed pages
  are reported as checksum mismatches and skipped until the feed is updated.
- aip.dfs.de blocks clients that request too fast (HTTP 403 on every URL, including the home page). The script
  waits 1 s before each request, downloads one plate at a time and stops at the first 403. Cached plates are kept,
  so just re-run later to fetch the rest.
- Check the DFS terms of use for the BasicVFR AIP before redistributing generated tiles.
