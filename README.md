# Faces

**Point it at any public photo gallery. See everyone who's in it. Find every photo you're in.**

Photo galleries are published as a flat wall of images. A conference, a wedding, a marathon, a school year — any of them routinely produces **one to several thousand photos in a single gallery**. If you're in there, today you have exactly one tool: scroll all of them and squint. Faces replaces that with a people index.

> Status: **runs locally.** One Python process, no Docker, no node. Not hosted yet.

---

## What it does

**Browse by face** — paste a gallery URL. Faces detects every face, groups them into unique people, and shows a wall of face thumbnails. Click one to see every photo that person appears in.

**Find me** — paste a gallery URL and add a selfie. Faces returns every photo in that gallery containing that face, ranked by confidence, split into *confident* and *possible* matches.

**Share it** — every indexed gallery gets a short link (`/g/abc123`) that opens straight to its face wall, and the home page lists everything indexed so far.

**Share a person** — one click copies a link to any face in a collection. Pasted into a chat or social app it unfurls as a card with that face and the collection's name; opening it lands on that person's photos. Links are keyed on a face, not a cluster, so they survive regrouping.

**Pool it** — a *collection* groups several indexed galleries into one set of people, with its own link (`/c/abc123`). It lists its sources, lets you search the whole pool with a selfie, and shows which gallery each match came from. Collections can't index anything; sources are added from the home page.

Every result links back to the original photo on the source gallery. Faces is a lens on someone else's gallery, not a copy of it.

## What it deliberately does not do

- **It does not name anyone.** Clusters are anonymous — "Person 7", never an identity. No name lookup, no social matching, no celebrity recognition.
- **It does not keep a face database.** Indexes are per-gallery and expire (default 30 days). There is no schema in which cross-gallery identities could accumulate.
- **It does not keep your selfie.** The upload and its embedding live in memory with a 30-minute TTL and are never written to disk.
- **It does not crawl.** Ingestion behaves like your browser: it opens the one page you pasted, scrolls it, and reads the images it shows. No site-wide crawling, no APIs, no keys — which is also why it works on any page you can open.

These are architectural choices, not policy promises — see [Tech Spec §11](docs/TECH-SPEC.md#11-privacy-security-and-compliance).

## How it works

```
URL ──▶ adapter ──▶ image manifest ──▶ fetch + dedupe ──▶ detect (SCRFD)
                                                              │
   face wall ◀── cluster (graph + centroid) ◀── embed (ArcFace 512-d) ──┘
                        │
   selfie ──▶ embed ──▶ ANN search (pgvector HNSW) ──▶ ranked photos
```

Next.js app for the UI, Python worker for the models, Postgres + pgvector for the index. Roughly **2 minutes and ~3 cents per 1,000 images** on a small GPU.

The part that actually decides whether this works is the **adapter layer** — turning an arbitrary gallery URL into a *complete* image manifest across platforms. Recognition is a commodity; reliable ingestion is not.

## Quickstart (local)

Requires Python 3.12+. First run downloads the face models (~280 MB) and a headless Chromium.

```bash
./run.sh
```

Then open <http://localhost:8000>, paste a gallery URL, and watch the wall fill in. Everything — the index, face crops, and thresholds — lives under `./data/` and `config/thresholds.yaml`.

**Expose one collection only.** `FACES_STANDALONE=<collection slug>` (set in `run.sh`, default `iia-summit-2026`) makes `/` redirect to that collection and strips every link off its page that leads elsewhere in the app; your working home — indexing, building collections — moves to `/admin`, which is not linked from anywhere public. Gallery pages stay reachable by their unguessable links but are not exposed.

**Nothing is ever deleted on its own.** Indexed galleries persist across restarts, a job interrupted by a restart resumes where it was, and re-submitting a URL that failed resumes rather than starting over. The only way an index goes away is the delete button (or removing `data/` yourself).

On an M1 Pro, expect ~1.2 s per image on CPU; a 1,000-image gallery is roughly 20 minutes. Faces stream in as they're found, so the wall is usable long before the run finishes.

## Hosting (search only)

The hosted profile is a **search box, not an indexer**: galleries are indexed on your machine and published to the server. Nothing in the repo contains photos or face data.

1. Deploy the repo as a container (there's a `Dockerfile`; no Playwright, no Chromium). Mount a persistent volume at **`/data`**, expose port **8000**, and set:
   - `FACES_ADMIN_TOKEN` — a long random secret; every write route and `/admin` require it
   - `FACES_STANDALONE=iia-summit-2026` — the one collection `/` shows
   - (indexing stays off unless `FACES_ALLOW_INDEX=1`)
2. First start downloads the face models (~280 MB) into `/data/models`. Until something is published, `/` shows a plain "nothing here yet".
3. From your machine, publish the index:
   ```bash
   FACES_ADMIN_TOKEN=… ./publish.sh https://faces.example.com
   ```
   That bundles `data/faces.db` + `data/crops/` (~150 MB for four galleries), uploads it over HTTPS, and the server swaps it in atomically — the previous index is kept as `data.prev` until the next publish. Re-run it whenever you index or regroup locally. No redeploy.
4. Your admin page on the server is `/admin?token=<the token>` — entered once, remembered by that browser only.

Selfie search is rate-limited per IP (20/min, burst 8). The originals are never copied: tiles and full photos load from the source gallery in the visitor's browser.

## Documentation

| Doc | What's in it |
|---|---|
| [**Executive Summary**](docs/EXEC-SUMMARY.md) | The whole project in two minutes — start here |
| [**PRD**](docs/PRD.md) | Why, what, how · 4 jobs-to-be-done · definition of done · definition of good · 45 test cases |
| [**Technical Spec**](docs/TECH-SPEC.md) | Architecture, adapters, pipeline, clustering, data model, evaluation, cost, privacy, risks |
| [**Design Guidelines**](docs/DESIGN-GUIDELINES.md) | Tokens, type, layout and components — the look of the source galleries, applied to a people index. The UI implements this and nothing else |

## Test fixtures

Faces is built against a **set** of galleries chosen for different shapes — candid and crowded, posed and clean, and at least one non-SmugMug source — precisely so it doesn't end up tuned to a single gallery. See [PRD §7](docs/PRD.md#fixtures).

## Status and next step

Runs locally (see Quickstart). The next deliverable is **P0**: the detect→embed→store path proven on a full real gallery, and — most importantly — the **labeled evaluation set**, which every later accuracy decision is judged against. See [Tech Spec §14](docs/TECH-SPEC.md#14-build-order).

## License

MIT © Oz Nakash
