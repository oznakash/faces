# Faces

**Point it at any public photo gallery. See everyone who's in it. Find every photo you're in.**

Photo galleries are published as a flat wall of images. A conference, a wedding, a marathon, a school year — any of them routinely produces **one to several thousand photos in a single gallery**. If you're in there, today you have exactly one tool: scroll all of them and squint. Faces replaces that with a people index.

> Status: **pre-build.** Docs first, code next. Nothing here runs yet.

---

## What it does

**Browse by face** — paste a gallery URL. Faces detects every face, groups them into unique people, and shows a wall of face thumbnails. Click one to see every photo that person appears in.

**Find me** — paste a gallery URL and add a selfie. Faces returns every photo in that gallery containing that face, ranked by confidence, split into *confident* and *possible* matches.

Every result links back to the original photo on the source gallery. Faces is a lens on someone else's gallery, not a copy of it.

## What it deliberately does not do

- **It does not name anyone.** Clusters are anonymous — "Person 7", never an identity. No name lookup, no social matching, no celebrity recognition.
- **It does not keep a face database.** Indexes are per-gallery and expire (default 30 days). There is no schema in which cross-gallery identities could accumulate.
- **It does not keep your selfie.** The upload and its embedding live in memory with a 30-minute TTL and are never written to disk.
- **It does not crawl what it shouldn't.** Ingestion is API-first and `robots.txt`-respecting: where a platform offers a sanctioned API, we use it instead of scraping.

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

## Documentation

| Doc | What's in it |
|---|---|
| [**Executive Summary**](docs/EXEC-SUMMARY.md) | The whole project in two minutes — start here |
| [**PRD**](docs/PRD.md) | Why, what, how · 4 jobs-to-be-done · definition of done · definition of good · 45 test cases |
| [**Technical Spec**](docs/TECH-SPEC.md) | Architecture, adapters, pipeline, clustering, data model, evaluation, cost, privacy, risks |

## Test fixtures

Faces is built against a **set** of galleries chosen for different shapes — candid and crowded, posed and clean, and at least one non-SmugMug source — precisely so it doesn't end up tuned to a single gallery. See [PRD §7](docs/PRD.md#fixtures).

## Status and next step

Pre-build. The next deliverable is **P0**: the first platform adapter, the detect→embed→store path over 200 images, and — most importantly — the **labeled evaluation set**, which every later accuracy decision is judged against. See [Tech Spec §14](docs/TECH-SPEC.md#14-build-order).

## License

MIT © Oz Nakash
