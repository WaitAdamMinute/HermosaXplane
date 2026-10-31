# Hermosa Beach Photoreal: X-Plane scenery

A photoreal overlay scenery pack for **Hermosa Beach, California** for X-Plane 11 and 12.
It replaces the default, misaligned scenery with:

| Feature | How |
|---|---|
| **Aerial photo ground** | USDA NAIP imagery (public domain, ~0.6 m) is draped over the whole city, the beach, the surf zone and the pier. It fades into X-Plane's own water about 650–900 m offshore. |
| **Roads and beach aligned to the photo** | Exclusion zones remove X-Plane's default roads, beaches, autogen, forests and objects inside the area. The roads and sand you see are the ones in the photo, so they line up exactly. |
| **3D buildings that match reality** | Every building is extruded from its real footprint and real height. In Hermosa Beach these come from the LA County LARIAC building import in OpenStreetMap: outlines traced from 4-inch aerial imagery, heights from LiDAR. Roofs are textured with the aerial photo itself. Walls use a generated stucco/storefront/glass atlas with lit windows at night. |
| **Hermosa Beach Pier** | Built in 3D from its OSM outline: a photo-textured deck on concrete pilings. |

Coverage: 33.849°N–33.876°N, 118.414°W–118.374°W (Manhattan Beach line to Herondo St, Strand to PCH and beyond). You can change this in [`config.toml`](config.toml).

## Getting the pack

### Option A: download a build from GitHub (easiest)

1. Go to **Actions → Build scenery pack → Run workflow** (pick `naip`, zoom `18`).
2. When it finishes (~10–20 min), open **Releases**. Download `zzz_Hermosa_Beach_Photoreal.zip`, plus `preview.jpg` and `render_check.jpg` if you want to check the result first.

### Option B: build it yourself

You need Python 3.11+ and an internet connection.

```bash
pip install -r requirements.txt
python -m hxp all          # fetch OSM + imagery, download DSFTool, build
```

Steps can also be run one at a time: `fetch-osm`, `fetch-imagery`, `tools`, `build`.
The output goes to `build/zzz_Hermosa_Beach_Photoreal/` (plus a `.zip`). `build/preview.jpg` overlays the building footprints and the coastline on the photo, so you can check the alignment.

## Installing

1. Unzip or copy `zzz_Hermosa_Beach_Photoreal` into `X-Plane 12/Custom Scenery/`.
2. Start X-Plane once. It adds the pack to `Custom Scenery/scenery_packs.ini`.
3. The pack must sit **above** any Ortho4XP or mesh packs in that file, and above `*GLOBAL_AIRPORTS*`. New packs are added at the top, so this is usually automatic.
4. Fly from KTOA (Torrance) or KLAX. Hermosa Beach is about 4 nm north of KTOA.

## How it works

```
OSM (Overpass) ─┬─ buildings (footprint + height) ──► one OBJ8 per building, roof UV = photo
                ├─ coastline ──► land polygon ──► photo alpha mask + drape extent
                └─ pier ──► deck + pilings OBJ8
NAIP imagery ───► 4096² DDS textures (photo on top, wall atlas in the bottom 512 rows)
                  draped .pol polygons with per-vertex UVs
                  ─────────────────────────────────────────────►  DSF text ─► DSFTool ─► +33-119.dsf
```

* `hxp/osm.py`: Overpass download, multipolygon assembly, coastline → land polygon
* `hxp/imagery.py`: Web-Mercator chunk grid, NAIP / XYZ download
* `hxp/ortho.py`: alpha mask, draped polygons, `.pol` files
* `hxp/buildings.py`: OBJ8 extrusion (earcut roofs, bay-subdivided walls, basements for slopes)
* `hxp/textures.py`: wall atlas, night-lighting texture, mipmapped DXT1/DXT5 DDS writer
* `hxp/dsf.py`: DSF text, DSFTool download/compile/verify
* `tests/make_synthetic.py` and `tests/render_check.py`: offline end-to-end test and software renderer

Each building is its own object, placed at its centroid. X-Plane then sets it on the terrain at the right elevation, even on Hermosa's hills, and walls continue a few metres below ground so nothing floats.

## Tuning (`config.toml`)

* **Higher-res photo:** set `zoom = 19`, `source = "xyz"` and an `xyz_url`. The default URL is Esri World Imagery. Check the provider's licence: most forbid redistribution, so keep such builds for personal use. NAIP is public domain.
* **Photos upside down in the sim?** Set `dds_flip = false`, or use `texture_format = "png"`.
* **Too much photo over the ocean?** Lower `sea_opaque` / `sea_fade`.
* **3D road network with traffic and streetlights (experimental):** set `[roads] enabled = true` and map OSM `highway` values to road type numbers from your X-Plane's `Resources/default scenery/1000 roads/roads.net`.

## Known limitations

* The footprint heights are LiDAR-accurate, but all roofs are flat with photo texture. Pitched roofs look correct from above and flat from the side.
* Wall textures are generic Hermosa-style stucco, storefront and glass. They are not photos of each building's façade.
* In this release the roads are the photo roads. The default 3D road network (cars, streetlights) is removed inside the area unless you enable the experimental OSM road network.

## Credits and licences

* Code: MIT.
* Map data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright). The building data originates from LA County LARIAC.
* NAIP imagery: USDA Farm Production and Conservation Business Center, public domain.
* DSFTool: Laminar Research xptools, downloaded at build time.
