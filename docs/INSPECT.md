# Inspect: what one photo gives away

`python -m faces.inspect photo.jpg` reads everything a single image file carries, locally, and ranks what it leaks. It was built after taking apart [theyseeyourphotos.com](https://theyseeyourphotos.com), Ente's demo of the same idea. The first half of this document is that teardown; the second is the tool.

## 1. How theyseeyourphotos.com works

Read from the shipped JavaScript bundles and one exchange with its API on 2026-10-18. No source is published; Ente's monorepo does not contain it.

**Stack.** A statically exported Next.js page on Cloudflare (`nextExport: true`, no server-side props). One backend host, `api.theyseeyourphotos.com`, behind Cloudflare, returning 403 to anything without the site's `Origin`. Analytics: PostHog (self-proxied at `ph.theyseeyourphotos.com`) and Sentry. The page text says "we use the Google Vision API" and the error string reads "Google's Vision API is overloaded right now"; the privacy policy says images are "relayed to Google through Cloudflare for AI processing".

**Client side, before upload** (all in the browser):

1. `exifr` parses the file with `tiff, exif, gps, iptc, xmp, icc, jfif, ihdr` enabled. The whole parsed object is kept in React state; four fields are sent on: `CreateDate`, `latitude`, `longitude`, and `Make + " " + Model`.
2. HEIF is converted to JPEG in the browser.
3. `browser-image-compression` re-encodes to **≤ 0.5 MB, ≤ 3000 px, quality 0.8**. This strips the original EXIF from the bytes that leave the browser, which is why the four fields are sent separately as form fields.
4. `POST /deductions` as `multipart/form-data`: `file`, `filename`, `created`, `latitude`, `longitude`, `camera`, `language` (`en` or `fr`).

**Server side** (black box; inferred from the response shape): the image plus those form fields go to a Google model, which is asked for a fixed set of sections. The response is one JSON object:

```
data.people        false → the UI shows "No people found" (the model is told to start from a face)
data.table         {"People", "Relationship", "Location", "Taken", "Setting", "Age estimate",
                    "Income band", "Ad segments", "Home region", "Faces", …}  free-form key→string
data.paras         [3 paragraphs of narrative]
data.inferences    {occupation, mentalHealth, negativeTrait, targetedAds, locationPhoto}
data.chips         [5 short observations]
data.summary       one sentence, the "share card" line
data.place         "[[Suburban California, USA]]"
data.links         []   (reserved for an "online footprint"; empty in every response I saw)
```

Anything the model considers sensitive is wrapped in `[[double brackets]]`; the client renders those as "taped over" until the visitor clicks *Reveal*. The client also has an alias map so it can read French or renamed table keys (`"Income band"`, `"tranche de revenus"` → `income`), and a filter that drops `none / unknown / not determinable` answers so the card never looks empty.

**Three things worth knowing** before copying the design:

- *The "Location" is usually the EXIF.* The example photo on their own homepage carries GPS (37.7314, −122.4858, a Nikon D850 in San Francisco); the card says "Suburban California". The model is handed the coordinates and dresses them up as a deduction from "lighting and fencing styles".
- *The profile is mostly prompt, not photo.* The same JPEG returned "Income band USD 60,000 to 90,000" in the table and "$75,000 to $110,000" in the prose, plus a political leaning, a religion, a sexual orientation and a list of cognitive biases. None of that is in the pixels. It is what the model produces when asked for it.
- *The static-map step leaks a Google Maps key.* The browser builds a `staticmap` URL with an `AIza…` key and asks `/map?path=` for a signed copy. Don't ship that pattern.

What is real and transferable: EXIF time, GPS and device; face count; visible text and scene; the "tape" interaction for sensitive fields.

## 2. The tool

```bash
./.venv/bin/python -m faces.inspect photo.jpg              # report
./.venv/bin/python -m faces.inspect photo.jpg --json       # everything, machine-readable
./.venv/bin/python -m faces.inspect photo.jpg --out DIR    # also report.json + the embedded EXIF thumbnail
./.venv/bin/python -m faces.inspect photo.jpg --no-faces   # metadata only, no model download
./.venv/bin/python -m faces.inspect photo.jpg --geocode    # + street address from OpenStreetMap  (network)
./.venv/bin/python -m faces.inspect photo.jpg --describe   # + what a vision model reads off the pixels (network, Claude)
```

Nothing leaves the machine unless you pass `--geocode` (one request to Nominatim with the coordinates) or `--describe` (a downscaled copy of the image to the Claude API; needs `pip install anthropic` and `ANTHROPIC_API_KEY`). Face detection uses the same InsightFace `buffalo_l` pack the indexer already downloads, plus its age/gender and 3-D landmark heads.

### Layers, in order of how hard they are to fake

| Layer | What it reads | What it tells you |
|---|---|---|
| **file** | size, SHA-256/MD5, sniffed MIME, dimensions, ICC profile description, JPEG quality estimate and chroma subsampling, the ordered list of JPEG segments, PNG text chunks, COM comments | The segment order (`JFIF → EXIF → XMP → ICC → Adobe APP14` vs `EXIF → DQT → SOF0`) fingerprints the last program that wrote the file. Quality 75 + 4:2:0 + no EXIF is a messenger. |
| **exif** | every tag in IFD0, Exif, GPS, Interop and IFD1, decoded; MakerNote and UserComment handled | Raw material for the next row. |
| **derived** | camera, lens, **body and lens serial numbers**, software, host computer name, owner/artist/copyright, Windows XP* tags, image unique ID; shutter/aperture/ISO/focal length/flash/program; taken/digitized/modified with sub-seconds and offsets, GPS UTC, **inferred home timezone** (local time minus GPS UTC), weekday; latitude/longitude with map links, altitude, heading, speed, horizontal error, destination | The facts. Serials link every photo from one camera to each other and to a purchase. Timezone plus weekday plus hour is a routine. |
| **xmp** | flattened from every packet in the file (no XML dependency); ~60 keys of interest: creator and contact info, keywords, edit history (`stEvt:softwareAgent`), document IDs, Lightroom crop rectangle and RAW filename, **face-region names** (`mwg-rs`, Microsoft People tags), `PersonInImage`, Google motion-photo/depth, IPTC digital source type (flags AI-generated media) | Names of the people in the photo survive here when the picture went through Picasa, Lightroom or Windows Photos. GPS often survives in XMP after an editor strips the EXIF copy. |
| **iptc** | the Photoshop IRB record: byline, credit, caption, city/state/country, keywords, instructions | Newsroom metadata; usually the photographer's name and location. |
| **embedded thumbnail** | the JPEG inside the EXIF, extracted and correlated against the main image | Cameras write it at capture; many editors never refresh it. Mismatch means a crop, a flip, a retouch, or the uncropped original is sitting in the file. Saved with `--out`. |
| **filename** | 25 patterns: `IMG_nnnn` Apple, `PXL_` Pixel, `IMG-date-WAnnnn` WhatsApp, `PHOTO-…` WhatsApp iOS export, `signal-`, `FB_IMG_`, `received_`, `Screenshot`, `DSC_`/`DSC`/`_DSC`/`DSCF` Nikon/Sony/Fuji, `GOPR`, `DJI_`, UUIDs, Unsplash slugs… | Which app produced the file, and whether to expect metadata at all. |
| **faces** | count, box, share of frame, detector score, age and gender estimate, head pose (yaw/pitch/roll) and facing, blur, "recognisable" (≥ 80 px and sharp) | Whether the photo can be matched against a face index like this project's. |
| **describe** (opt-in) | scene, setting, people (age range, clothing/uniform, visible logos and badges, activity), every legible string verbatim, location clues and time clues each with confidence, objects and brands, a ranked list of sensitive items (addresses, plates, screens, documents, keys, reflections, children, interiors), and whether the pixels agree with the metadata | The pixel half. The prompt forbids guesses at ethnicity, nationality, religion, politics, orientation, income, health, or identity by name. |

The report opens with **What it gives away**: the leaks above, ranked high / medium / low, in one screen.

### Sample

The exiftool test image from an iPhone 7, 8×8 pixels, run with `--geocode`:

```
WHAT IT GIVES AWAY
  [high  ] Exact GPS position: 53.382847, -1.456678 (±165.0 m) — Bard Street, Victoria Quays, Castlegate Quarter, Park Hill, Sheffield, South Yorkshire, England, S2 5PA, United Kingdom
  [medium] Capture time: 2016-09-28T18:18:05.813 (Wednesday), home timezone UTC+01:00
  [low   ] Device: Apple iPhone 7, iPhone 7 back camera 3.99mm f/1.8
  [low   ] Software: 10.0.2
```

The site's own example photo, 2049×1366, face detection on:

```
WHAT IT GIVES AWAY
  [high  ] Exact GPS position: 37.731411, -122.485794
  [high  ] 3 face(s), 3 recognisable: ~41y male camera; ~22y female three-quarter; ~4y male camera
  [low   ] Device: Nikon D850

FACES  (3 detected)
  bbox [878, 486, 1133, 846]   3.29% of frame  score 0.857  ~41y male    camera yaw 13.8   blur 183.9  recognisable
  bbox [1166, 546, 1357, 805]  1.77% of frame  score 0.863  ~22y female  three-quarter    blur 133.2  recognisable
  bbox [717, 141, 912, 385]    1.7% of frame   score 0.888  ~4y male     camera yaw -12.6  blur 62.2   recognisable
```

The second face is a one-year-old. The age/gender head is a guess and the report says so; it is there to show that the guess gets made, not to be believed.

### What it deliberately does not do

Same rules as the rest of Faces. No identity: faces are counted and measured, never matched or named. No protected-class inference in the vision prompt. No face database: nothing is written except the report you ask for. The one network call that sends the picture anywhere is `--describe`, and it is off by default.
