# Faces

**Point it at a public photo gallery. See everyone who's in it. Find every photo you're in.**

Event photography is published as a flat wall of images — the gallery this project is being built against holds **1,174 photos from a single reception**. If you attended, there is exactly one way to find yourself today: scroll all 1,174 and squint. Faces replaces that with a people index.

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
- **It does not crawl what it shouldn't.** Ingestion is API-first and `robots.txt`-respecting. For the reference gallery that means SmugMug's sanctioned `/api/v2`, not scraping.

These are architectural choices, not policy promises — see [Tech Spec §11](docs/TECH-SPEC.md#11-privacy-security-and-compliance).

## How it works

```
URL ──▶ adapter ──▶ image manifest ──▶ fetch + dedupe ──▶ detect (SCRFD)
                                                              │
   face wall ◀── cluster (graph + centroid) ◀── embed (ArcFace 512-d) ──┘
                        │
   selfie ──▶ embed ──▶ ANN search (pgvector HNSW) ──▶ ranked photos
```

Next.js app for the UI, Python worker for the models, Postgres + pgvector for the index. Roughly **2.5 minutes and ~3 cents** to index the 1,174-photo reference gallery on a small GPU.

## Documentation

| Doc | What's in it |
|---|---|
| [**Executive Summary**](docs/EXEC-SUMMARY.md) | The whole project in two minutes — start here |
| [**PRD**](docs/PRD.md) | Why, what, how · 4 jobs-to-be-done · definition of done · definition of good · 43 test cases |
| [**Technical Spec**](docs/TECH-SPEC.md) | Architecture, adapters, pipeline, clustering, data model, evaluation, cost, privacy, risks |

## Reference gallery

Built and measured against [IIA AI Summit Silicon Valley — Opening Reception](https://www.johnwernerphotography.com/IIA-AI-Summit-Silicon-Valley-Sept-13-15-2026/Opening-Reception) — SmugMug, album `B2cCGn`, 1,174 images. Every performance number in these docs refers to that workload.

## Status and next step

Pre-build. The next deliverable is **P0**: the SmugMug adapter, the detect→embed→store path over 200 images, and — most importantly — the **labeled evaluation set**, which every later accuracy decision is judged against. See [Tech Spec §14](docs/TECH-SPEC.md#14-build-order).

## License

MIT © Oz Nakash
