# Faces — Technical Specification

| | |
|---|---|
| **Status** | Draft v0.1 — pre-build |
| **Owner** | Oz Nakash |
| **Last updated** | 2026-09-20 |
| **Implements** | [PRD](./PRD.md) |
| **Benchmark workload** | A 1,000-image public gallery — the unit all speed and cost figures use (fixtures: [PRD §7](./PRD.md#fixtures)) |

---

## 1. System overview

```
                 ┌──────────────────────────────────────────────┐
  Browser  ──────▶  Next.js app  (UI, API routes, SSE stream)    │
   (URL,   ◀──────┤  Vercel / Node                               │
   selfie)        └───────┬──────────────────────────▲───────────┘
                          │ enqueue                  │ read
                          ▼                          │
                  ┌───────────────┐          ┌───────┴──────────┐
                  │ Redis queue   │          │ Postgres 16      │
                  │ (job state)   │          │ + pgvector       │
                  └───────┬───────┘          └───────▲──────────┘
                          │ consume                  │ write
                          ▼                          │
                  ┌────────────────────────────────────────────┐
                  │ Python worker (FastAPI + RQ)               │
                  │  adapters → fetch → detect → embed →       │
                  │  cluster → crop                            │
                  │  InsightFace (SCRFD + ArcFace), ONNXRuntime│
                  └───────┬────────────────────────────────────┘
                          │ put crops
                          ▼
                  ┌───────────────┐
                  │ Object store  │  (R2 / S3) — face crops + thumbnails only
                  └───────────────┘
```

Two services, one database. The Next.js app never runs a model; the worker never serves a page.

### Why this shape

- **Split runtimes because the workloads are opposite.** The UI is latency-sensitive, bursty, and stateless. Indexing is throughput-sensitive, long-running, and stateful. Serverless functions are a bad host for an 8-minute GPU job; a GPU box is a bad host for a Next.js edge route.
- **Postgres + pgvector instead of a dedicated vector DB.** At the scale of this product — a few hundred thousand vectors per gallery at the very top end — HNSW in pgvector is comfortably fast, and one datastore beats two. Revisit above ~10M vectors.
- **Object store holds only derived crops and thumbnails, never full source images.** We are a lens on someone else's gallery; mirroring it is both a storage cost and a rights problem.

---

## 2. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend / API | **Next.js 15 (App Router), TypeScript, Tailwind** | Matches existing tooling; SSE from a route handler for progress |
| Worker | **Python 3.12, FastAPI, RQ** | The ML ecosystem is Python; RQ over Celery for operational simplicity |
| Detection | **SCRFD-10G** (InsightFace `buffalo_l`) | ~99% AP on WIDER FACE easy/medium; handles small and crowded faces |
| Embedding | **ArcFace R100** (InsightFace `buffalo_l`), 512-d, L2-normalized | 99.8% LFW; the de-facto open baseline |
| Runtime | **ONNXRuntime** (CUDA EP on GPU, CPU EP fallback) | Same weights both paths; no PyTorch in prod |
| Database | **Postgres 16 + pgvector 0.7** (HNSW) | Vectors, metadata, job state of record |
| Queue | **Redis 7 + RQ** | Job dispatch, progress counters, rate limits |
| Object store | **Cloudflare R2** (S3 API) | Zero egress fees; crops are read far more than written |
| Headless fetch (fallback adapter only) | **Playwright** | Only for galleries with no sanctioned API |
| Hosting | App: Vercel · Worker: Fly.io / Runpod GPU (L4) with CPU autoscale fallback | |

**Rejected:** AWS Rekognition Collections (~$1.00 per 1,000 images vs ~$0.03 self-hosted, and embeddings never leave the vendor — kept as a documented failover, see §9). face_recognition/dlib (materially weaker on profile and low-light faces). A universal headless scraper as the primary path (slow, fragile, and disrespects sanctioned APIs).

---

## 3. Ingestion — the adapter layer

This is the part that decides whether the product works. Recognition is solved; *completely and legitimately enumerating an arbitrary gallery* is not.

### 3.1 Resolution order

```
URL → normalize → adapter match (by host + URL shape)
   1. Platform API adapter   (SmugMug, later Zenfolio/Flickr/Pixieset)
   2. Structured-data adapter (JSON-LD ImageGallery, OpenGraph, RSS)
   3. Static HTML adapter     (<img>, <a href="*.jpg">, srcset, <picture>)
   4. Headless adapter        (Playwright, scroll-to-bottom, network sniff)
   → image manifest: [{source_url, page_url, width, height, caption?}]
```

Each adapter reports `confidence` and `completeness_known` (does it know the true total?). A platform-API adapter usually knows — SmugMug reports an `ImageCount`, so we can assert *N in, N accounted for* and fail the job on any shortfall. An HTML or headless adapter usually cannot, and must say so rather than let a partial crawl look complete. **Silent truncation is the single most dangerous failure mode in this system**: a gallery where only the first 20 lazy-loaded images were read returns results that look perfectly fine and are mostly missing.

### 3.2 Worked example: a platform-API adapter (SmugMug)

SmugMug is the first adapter because it is a common host for event galleries and because it demonstrates the general pattern — *find the sanctioned interface, use it, and verify completeness against the platform's own count*. The same shape applies to Zenfolio, Flickr and Pixieset.

A representative SmugMug-hosted site serves this `robots.txt`:

```
user-agent: *
disallow: /
...
allow: /api/v2
```

Generic crawling is disallowed; the v2 API is explicitly allowed. So:

1. `GET` the gallery page once (a single user-directed fetch, not a crawl).
2. Extract `AlbumKey` from the embedded bootstrap JSON — the page also carries `ImageCount`, which becomes the completeness assertion. (Verified against the fixture galleries in [PRD §7](./PRD.md#fixtures).)
3. Paginate `GET /api/v2/album/{AlbumKey}!images?start=N&count=100` with an anonymous SmugMug API key (free, application-level, never the user's).
4. Each `AlbumImage` yields size variants (`S/M/L/XL`) and a `WebUri` for the deep link home. **Fetch the `M` variant** (~800px long edge, ~150 KB) — enough for detection and embedding at a fraction of the bytes.
5. Assert `len(manifest) == ImageCount`; any shortfall is a hard job error, not a silent truncation.

**The generalizable lesson:** the gallery page itself is JS-rendered — a naive HTML fetch yields a handful of image URLs out of a thousand and *looks like it worked*. Every adapter must therefore either verify against a source-reported total or declare its count unverified.

> **Open dependency, generalizes to every platform adapter:** the sanctioned API needs an application-level key, and `/api/v2` returns `401` without one (verified). **No adapter ships until its sanctioned access path is secured** — otherwise the only route left is the headless fallback, which is slower and carries a robots-compliance decision that has to be made explicitly rather than by default.

### 3.3 Crawl policy

- Respect `robots.txt` for all non-API paths; cache per host for 24h.
- Identify honestly: `User-Agent: FacesBot/0.1 (+https://github.com/oznakash/faces)`.
- Per-host concurrency cap of 4, ≥ 100ms between requests, exponential backoff on 429/503 with `Retry-After` honored.
- Conditional requests (`ETag`/`If-None-Match`) on re-index.
- Hard caps per job: 5,000 images, 4 GB transferred, 15 minutes wall clock — whichever hits first aborts cleanly with partial results retained and labeled.

---

## 4. Processing pipeline

Per image, all steps idempotent and keyed by `content_sha256`:

| # | Step | Detail |
|---|---|---|
| 1 | **Fetch** | Streamed, 20s timeout, max 25 MB, content-type verified |
| 2 | **Hash & dedupe** | SHA-256 of bytes; a repeat hash short-circuits the whole pipeline |
| 3 | **Decode & normalize** | EXIF orientation applied; resize so long edge ≤ 1600px (a no-op when the adapter can request a ~800px variant, as SmugMug's `M` does) |
| 4 | **Detect** | SCRFD, `det_thresh=0.5`, `det_size=(1024,1024)`; returns bbox, 5-point landmarks, score |
| 5 | **Quality filter** | Reject face if: bbox short edge < 40px, detection score < 0.6, Laplacian blur variance < 25, or yaw estimate > 75°. Rejected faces are stored with `excluded_reason` — counted, never silently dropped |
| 6 | **Align** | Similarity transform from the 5 landmarks to the canonical 112×112 ArcFace template |
| 7 | **Embed** | ArcFace R100 → 512-d float32, L2-normalized so cosine similarity is a dot product |
| 8 | **Crop** | 1.4× bbox square crop → 256px WebP → object store; this is the face-wall thumbnail |
| 9 | **Persist** | One `faces` row per face; batched inserts of 64 |

Batching: images processed in batches of 32 through both models. Worker concurrency = 4 fetch threads feeding 1 inference process (GPU) or 2 (CPU) — fetching, not inference, is the bottleneck on a warm CPU path.

---

## 5. Clustering (the face wall)

**Problem shape:** unknown number of people, heavy class imbalance (the keynote speaker appears 200 times, a passerby once), and a hard requirement that over-merging is worse than over-splitting.

**Algorithm — threshold graph + centroid consolidation:**

1. Build a k-NN graph over all face embeddings in the gallery (`k=30`, HNSW via pgvector).
2. Keep edges with cosine similarity ≥ `T_link` (**0.55**, calibrated in §7).
3. Take connected components — but reject any merge that would join two components whose *centroids* are below `T_merge` (**0.45**). This blocks the classic chain failure where A~B~C merges two people through an ambiguous middle face.
4. Keep a singleton component only if the face is high quality (`det_score ≥ 0.75` and bbox short edge ≥ 80px). A genuine one-appearance attendee earns a slot on the wall; a marginal stray face does not. Singletons are never force-merged (T-B5).
5. Per cluster, compute a centroid and pick the **representative crop**: the face with the highest `0.6·detection_score + 0.4·cosine_to_centroid`, tie-broken by bbox area. This is what makes the wall legible.
6. Order the wall by photo count descending.

**Incremental behavior:** clustering runs on a 250-face cadence during indexing so the wall populates live, and once at the end for the final assignment. Streaming assignments are marked `provisional`; the final pass is authoritative and deterministic (sorted inputs, fixed seed) per T-B10.

**Known limits, accepted for v1:** identical twins, heavy occlusion, and faces only ever seen in extreme profile. Profile-only faces are left as singletons rather than risked into a wrong merge — deliberately trading recall for precision, per the PRD's protected metric.

---

## 6. Selfie search

1. Decode upload (max 10 MB, JPEG/PNG/HEIC/WebP) through PIL: apply EXIF orientation (phone photos are stored rotated), HEIC via `pillow-heif`, downscale past 2000 px; strip EXIF **including GPS** before any processing.
2. Detect faces — **with a padding ladder.** SCRFD is trained on faces small relative to the frame; a selfie's face fills 50%+ and, upscaled to the 1024 px detection window, exceeds every anchor (measured on the portrait fixture: 1 face found at 33% of frame, 0 at 50%). So when detection returns nothing, the frame is padded with a neutral border (50%, 100%, 175% of its size) and retried, boxes mapped back. Indexing gets one padded retry too, for frame-filling portraits. Zero after the ladder → `NO_FACE_DETECTED` with retake guidance (T-C4). More than one → return the candidate crops and let the user pick, defaulting to largest-area (T-C3).
3. Embed with the identical align → ArcFace path as indexing. *Any divergence between index-time and query-time preprocessing silently destroys accuracy; this is enforced by a shared code path, not by convention.*
4. ANN query: `SELECT ... ORDER BY embedding <=> $1 LIMIT 500` over the gallery's faces, HNSW `ef_search=100`.
5. Two-tier thresholds on cosine similarity:
   - **Confident:** ≥ `T_hit` (**0.62**)
   - **Possible:** `0.50 ≤ s < 0.62` → separate tray, labeled
   - Below 0.50: discarded, never shown.
6. Group faces by `image_id`, score each photo by its best matching face, rank descending.
7. Delete the query embedding when the session ends or after 30 minutes (D12/T-D1). It is never written to Postgres or disk — it lives in the request and in a Redis key with a TTL.

Thresholds are **configuration, not constants** (`config/thresholds.yaml`), because they are the single highest-leverage tuning surface and they must be re-calibrated whenever the model changes.

---

## 7. Accuracy, evaluation, and calibration

Shipping face matching without a labeled eval set is guessing. The eval harness is P0 scope, not P3.

**Eval set:** 300 photos sampled across fixture galleries ([PRD §7](./PRD.md#fixtures)) — weighted toward the candid, crowded shape, because that is where accuracy is actually decided — hand-labeled with face bounding boxes and anonymous person IDs (`person_01`…), plus 15 held-out "selfie" crops — 10 of people who are in the set, 5 of people who are not (the negative controls that catch nearest-neighbor fallback, T-C2).

**Metrics:**

| Metric | Definition | Gate |
|---|---|---|
| Detection recall | detected / human-labeled faces passing the quality filter | ≥ 0.95 |
| Search precision | correct / returned in the confident tray | **≥ 0.98 (protected)** |
| Search recall | correct returned / all true photos of that person | ≥ 0.90 |
| Cluster purity | weighted mean of the dominant-person fraction per cluster | ≥ 0.90 |
| Cluster fragmentation | mean clusters per real person | ≤ 2.0, target 1.0 |
| Negative-control rate | confident hits for people not in the gallery | **0** |

**Calibration:** `T_hit`, `T_link`, and `T_merge` are swept over 0.40–0.75 in 0.01 steps against the eval set. `T_hit` is chosen as the **lowest** threshold whose precision still clears 0.98 — maximizing recall subject to the precision floor, never the other way round.

**In CI:** the harness runs on every change to models, thresholds, or preprocessing, and fails the build if any gate regresses. The eval set is stored as crops + labels, not as redistributed full-resolution photos.

---

## 8. Data model

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per submitted gallery URL (canonicalized).
CREATE TABLE galleries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  url_canonical   text NOT NULL UNIQUE,
  slug            text UNIQUE,                -- short shareable key: /g/{slug}
  host            text NOT NULL,
  adapter         text NOT NULL,              -- 'smugmug' | 'jsonld' | 'html' | 'headless'
  title           text,
  expected_count  integer,                    -- NULL when the adapter cannot know
  status          text NOT NULL,              -- queued|ingesting|processing|ready|partial|failed
  failure_code    text,                       -- machine-readable; drives the user-facing message
  images_total    integer DEFAULT 0,
  images_done     integer DEFAULT 0,
  faces_found     integer DEFAULT 0,
  cost_cents      integer DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  indexed_at      timestamptz,
  expires_at      timestamptz NOT NULL        -- TTL, default now() + 30 days (D13)
);

CREATE TABLE images (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gallery_id     uuid NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  source_url     text NOT NULL,               -- the image file
  page_url       text NOT NULL,               -- the deep link home (D3)
  content_sha256 char(64),
  width          integer,
  height         integer,
  status         text NOT NULL DEFAULT 'pending',
  error_code     text,
  UNIQUE (gallery_id, source_url)
);
CREATE INDEX ON images (gallery_id, status);
CREATE INDEX ON images (content_sha256);      -- dedupe (T-A8)

CREATE TABLE clusters (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gallery_id    uuid NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  label         text,                         -- 'Person 7'; never a real name (PRD §1)
  face_count    integer NOT NULL DEFAULT 0,
  image_count   integer NOT NULL DEFAULT 0,
  rep_face_id   uuid,                    -- no FK: would cycle with faces.cluster_id
  centroid      vector(512),
  provisional   boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON clusters (gallery_id, image_count DESC);

CREATE TABLE faces (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gallery_id      uuid NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  image_id        uuid NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  bbox            integer[4] NOT NULL,
  det_score       real NOT NULL,
  blur_score      real,
  embedding       vector(512),                -- NULL when excluded
  crop_key        text,                       -- object-store key
  cluster_id      uuid REFERENCES clusters(id) ON DELETE SET NULL,
  excluded_reason text,                       -- 'too_small'|'low_score'|'blurry'|'extreme_pose'
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON faces USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
CREATE INDEX ON faces (gallery_id, cluster_id);
CREATE INDEX ON faces (image_id);
```

**Deliberately absent: any table for selfies, users, or persistent identities.** A selfie embedding exists only in a Redis key with a 30-minute TTL. There is no schema in which a cross-gallery face database could accidentally accumulate — the safest privacy control is the one that is structurally impossible to violate.

Deletion is `DELETE FROM galleries WHERE id = $1` — cascades handle everything in Postgres, and a companion job deletes the crop prefix from the object store (D13/T-D3).

---

## 9. Performance and cost budget

**Benchmark: 1,000 images, ~3.5 faces each (~3,500 faces).** Scale linearly for larger galleries.

| Stage | GPU path (L4) | CPU path (8 vCPU) |
|---|---|---|
| Fetch (4 concurrent, ~150 KB medium variant) | ~85s | ~85s |
| Detect + embed | ~50s @ ~20 img/s | ~333s @ ~3 img/s |
| Cluster (~3,500 vectors) | ~5s | ~10s |
| Crop + upload | ~25s | ~40s |
| **Total (overlapped)** | **~2 min** | **~6.5 min** |

Both paths clear the ≤ 7 min per-1,000 gate (D10); the CPU path clears it with little margin, which is why fetch and inference overlap rather than run in sequence — and why a 2,000-image gallery needs the GPU path to stay inside its SLA. Time-to-first-face is governed by the first completed batch — **< 15s on either path**.

| Engine | Cost per 1,000 images | Cost per 10,000 |
|---|---|---|
| Self-hosted GPU (L4 @ ~$0.80/hr) | **~$0.027** | ~$0.27 |
| Self-hosted CPU (8 vCPU @ ~$0.34/hr) | ~$0.037 | ~$0.37 |
| AWS Rekognition `IndexFaces` @ $0.001/img | ~$1.00 | ~$10.00 |

Self-hosting is ~35× cheaper and keeps embeddings in our control. Rekognition stays documented as a failover behind the same worker interface, so a model outage is a config change, not a rewrite.

**Storage:** ~3,500 crops × ~18 KB ≈ 63 MB per 1,000 images, plus ~7 MB of vectors. Negligible, and it expires.

**Per-job cost ceiling:** default 200 cents. Exceeding it aborts with partial results retained (T-A10, D18).

---

## 10. API

All app routes are Next.js route handlers; the worker is reachable only from the app (private networking).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/galleries` | `{url}` → `{gallery_id, status, cached}`. Idempotent on canonical URL (T-A2/D6) |
| `GET` | `/g/:slug` | **Shareable short link** — the app, opened straight to that gallery's face wall |
| `GET` | `/api/galleries/:id` | Status, counts, adapter, failure code. Every `:id` here also accepts the short slug |
| `GET` | `/api/galleries/:id/events` | **SSE**: `progress`, `cluster_added`, `cluster_updated`, `done`, `error` (D5) |
| `GET` | `/api/galleries/:id/people` | Face wall: clusters with rep crop URL + image count, ordered |
| `GET` | `/api/people/:cluster_id/photos` | Photos for a person, each with `page_url` and match score |
| `POST` | `/api/galleries/:id/search` | `multipart` selfie → `{confident:[], possible:[], query_id}` |
| `POST` | `/api/galleries/:id/search/select-face` | Disambiguates a multi-face selfie (T-C3) |
| `DELETE` | `/api/galleries/:id` | Immediate deletion of index, crops, embeddings (D13/T-D3) |
| `GET` | `/` · `/admin` | With `FACES_STANDALONE` set, `/` redirects to that collection; `/admin` is the unlinked working home |
| `GET` | `/c/:slug` | A **collection's** shareable link: sources, pooled face wall, selfie search — no indexing |
| `POST` `GET` `PATCH` | `/api/collections`, `/api/collections/:key` | Create (name + gallery keys + optional link name) / read / rename or change link name. Link names are `[a-z0-9-]`, e.g. `iia-summit-2026`; the random 6-char slug is only the default |
| `POST` `DELETE` | `/api/collections/:key/sources[/:gallery]` | Add or remove a source; regroups the pool |
| `GET` | `/api/collections/:key/people`, `…/people/:id/photos` | Pooled people; a person's photos across sources, each labeled |
| `POST` | `/api/collections/:key/search` | Selfie against the pooled faces; results carry their source |
| `POST` | `/api/share` | Creates a stable share link for a selected photo set (D7) |

**Errors** are `{error_code, message, retry_after?}` with machine-readable codes — `ROBOTS_DISALLOWED`, `GALLERY_PROTECTED`, `NO_IMAGES_FOUND`, `NO_FACE_DETECTED`, `RATE_LIMITED`, `COST_CEILING`, `SOURCE_UNAVAILABLE` — each mapped to a specific user-facing sentence (D19/T-E6).

**Rate limits** (Redis token bucket): 5 gallery submissions per IP per hour, 30 selfie searches per IP per hour, 4 concurrent fetches per source host, and a global concurrent-job cap.

---

## 11. Privacy, security, and compliance

Face embeddings are **biometric identifiers**. Under GDPR Art. 9 they are special-category data; Illinois BIPA and Texas CUBI impose consent and retention duties with private rights of action (BIPA) attached. This is the highest-risk surface in the product, and the architecture — not the privacy policy — is where it gets managed.

**Structural controls (what the code makes impossible):**
- No user accounts, no persistent identity table, no cross-gallery index. There is no schema for a face database to accumulate in.
- Selfie embeddings live only in a TTL'd Redis key; there is no code path that writes them to Postgres or disk.
- Clusters are anonymous and gallery-scoped. No name enrichment, no external identity lookup, ever.
- Every index has a non-null `expires_at`; a scheduled sweeper enforces it (T-D2).
- EXIF, including GPS, is stripped from uploads before processing.

**Policy controls:**
- Plain-language privacy notice shown **before** the first upload (D14/T-D7).
- Takedown path for gallery owners and individuals, with a published SLA; domain denylist checked pre-fetch (T-D4).
- Robots and ToS compliance as a hard precondition of ingestion (D16/T-A9).
- **Open legal questions carried from PRD §8:** consent-vs-takedown basis (Q1) and geo-gating Illinois/Texas (Q2). Both need counsel before public launch — flagged as launch blockers, not backlog items.

**Security:** signed, short-lived URLs for crops; SSRF protection on ingestion (block private/link-local ranges, cap redirects at 3, re-validate the host after every redirect); magic-byte content-type validation on uploads; decompression-bomb limits on decode; the worker holds no inbound public surface.

---

## 12. Observability

- **Per job:** images fetched / skipped / failed by reason, faces detected / excluded by reason, cluster count, wall-clock per stage, cost in cents.
- **Golden signals:** p50/p95 time-to-first-face, p95 index completion, p95 selfie query latency, job failure rate by `error_code`, cache-hit rate.
- **Quality drift:** weekly eval-harness run against the frozen eval set, alerting on any gate regression (§7).
- **Counter-metrics from PRD §6** — takedown volume, cost-ceiling aborts, clusters-per-person — are dashboarded alongside the growth metrics, on the same screen, deliberately.

---

## 13. Risks

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| A platform's sanctioned API needs credentials we can't get (or revokes them) | That whole platform becomes unreachable via the legitimate path — not one gallery, a category | Medium | Secure the key as a gate before the adapter ships; headless fallback only with an explicit robots decision; never let one platform be a single point of failure |
| Precision below 0.98 on event-quality photos | Core trust broken | Medium | Two-tier thresholds; precision is the optimization constraint, not a target; eval gate in CI |
| A platform blocks `FacesBot` | Ingestion breaks for every gallery on that platform | Medium | Honest UA, conservative rates, API-first; partnership conversation over evasion — we will not rotate UAs to get around a block |
| BIPA/CUBI exposure | Legal, existential for a side project | Low–Medium | Anonymous clusters, TTLs, no persistence, counsel before launch, geo-gating if advised |
| CPU-only hosting can't hold the SLA on larger galleries | Missed D10 | Medium | Overlap fetch and inference; scale to GPU on demand; smaller `M` variants |
| Someone uses it to stalk an individual | Serious harm, reputational | Low–Medium | No naming, no cross-gallery search, rate limits, takedown path, denylist, abuse logging |
| Over-splitting clusters makes the wall noisy | Face wall (JTBD-2) feels broken | High | Centroid consolidation pass; fragmentation is an explicit eval gate (≤ 2.0) |

---

## 14. Build order

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| **P0** | First platform adapter (API key registered), fetch → detect → embed → store over 200 images, **eval set labeled and harness running**, FIX-3 candidate selected | Detection recall ≥ 0.95; thresholds calibrated |
| **P1** | Clustering, face wall UI, SSE progress, caching, full runs over **every fixture** including a non-SmugMug one | D1–D3, D5–D6, D10–D11 pass on all fixtures |
| **P2** | Selfie search, two-tier results, multi-face disambiguation, TTL enforcement | D4, D8–D9, D12 pass; T-C1–T-C9 green |
| **P3** | Download/share, rate limits, privacy notice, takedown path, denylist, cost ceiling, second platform adapter | D7, D13–D20 pass; legal Q1/Q2 resolved |

**P0 is not a throwaway spike.** It carries the eval set, which is the artifact every later decision is judged against — build it first or tune blind. And the fixture set is not decoration: a system tuned against a single gallery is a demo, not a product, so a non-SmugMug fixture gates P1's exit.

---

## 15. Local profile (what runs today)

The production shape in §1–§2 is the target. What exists now is a **single-process local build** that keeps the same pipeline, thresholds, and table shapes while stripping every piece of infrastructure that only matters at scale. The point is to prove accuracy and ingestion on real galleries before spending anything on hosting.

| Concern | Production (§2) | Local profile | Why it's fine locally |
|---|---|---|---|
| Vector search | Postgres + pgvector HNSW | **SQLite + numpy** — embeddings as float32 BLOBs, cosine as a matrix multiply | A few thousand vectors is ~8 MB; a full scan is ~1 ms. HNSW buys nothing below ~1M vectors |
| Queue | Redis + RQ | A background thread per gallery, job state in the `galleries` row | One user, one machine |
| Progress | SSE from a Next.js route | SSE from FastAPI, in-process pub/sub | Same wire format; the UI won't change |
| Frontend | Next.js on Vercel | **One zero-build HTML page** served by FastAPI | No node, no bundler, no build step |
| Crops | R2 object store | `./data/crops/` | Served as static files |
| Inference | GPU worker | ONNX Runtime on CPU (M1 Pro: ~1.25 s/image at `det_size=1024`; CoreML EP measured no faster) | Overlaps with fetching; wall fills as it runs |

**Ingestion, as built.** Playwright loads the page, scrolls until the height stops growing, and harvests every `<img>` (largest `srcset` candidate wins) plus the tile's link. Size variants of the same photo are collapsed, keeping the largest. Then each URL is **upgraded**, because grids lazy-load thumbnails that are useless for recognition — on the candid fixture the grid serves 417 `S` and 757 `M` (600 px) tiles, and a 600 px crowd shot yields 20 detections and **zero** usable faces. Upgrade order:

1. A host-specific **URL-rewrite rule** (SmugMug: size letter → `X3`, 1600 px; the path hash is not size-bound — verified).
2. Else the linked photo page's **`og:image`** (generic; on SmugMug it advertises a 1024 px `XL`).
3. Else the harvested thumbnail.

The pipeline tries those in order and falls back on any fetch error, so an upgrade rule that breaks degrades to the thumbnail rather than to a failed image. At 1600 px the same crowd shot yields 6 usable faces of 20 detected — the rest are genuinely too small, and are counted as `too_small` rather than dropped.

Completeness: the harvester reads the page's own reported total where one exists and asserts against it. On the candid fixture: **1,174 harvested of 1,174 reported, in 53 s.**

**First full run, candid fixture (FIX-1), M1 Pro CPU, default thresholds:**

| | |
|---|---|
| Images | 1,174 of 1,174, 0 failed — status `ready` |
| Wall clock | ~16 min end to end (≈0.8 s/image effective; fetch overlaps inference) |
| Detections | 10,470 faces |
| Usable (embedded) | 2,612 — excluded: 7,585 `too_small`, 163 `blurry`, 110 `low_score` |
| People | 210 clusters; largest 142 / 115 / 114 / 103 photos; 34 singletons |
| Clustered | 2,386 of 2,612 usable faces (226 dropped as low-quality singletons) |

**Second full run, portrait fixture (FIX-2), same machine, same thresholds:**

| | |
|---|---|
| Images | 1,834 of 1,834, 0 failed — status `ready` |
| Wall clock | 11.1 min (faster than the smaller candid set: ~1.2 faces/image to embed instead of ~9) |
| Detections | 2,257 faces → 2,251 usable; excluded: 4 `too_small`, 2 `low_score`, 0 `blurry` |
| People | 251 clusters; largest 90 / 37 / 36 / 36; 10 singletons |

The contrast is the point of having two fixtures. On posed portraits **99.7% of detections are usable**, versus 25% on the candid set — same model, same thresholds, same 1600 px upgrade. The quality filter is doing what it should: it is the photographs that differ, not the pipeline. The portrait set's flatter cluster curve (90, then 37, 36, 36…) is also what a "one portrait session per attendee" gallery should look like; the 90-photo outlier is worth a look on the wall.

Two readings of the candid set. First, **72% of detections are background crowd** — faces under 40 px even at 1600 px. That is the nature of reception photography, not a bug, and it is why the exclusion is counted rather than hidden. Second, the cluster-size curve (142, 115, 114, 103, 80…) is the shape you'd expect of an event — a handful of hosts and speakers, then a long tail — which is weak but real evidence that clustering is not wildly over-merging. Whether it's over-*splitting* needs the eval set (§7); the wall is the fastest way to eyeball it.

**The tuning loop.** `POST /api/galleries/{id}/recluster` re-reads `config/thresholds.yaml` and re-clusters without re-indexing, so threshold changes take seconds to evaluate, not twenty minutes. This is how `t_link` / `t_merge` / `t_hit` get calibrated once the eval set exists (§7).

**Share links** are built: each gallery gets a 6-character slug from an alphabet without 0/o/1/l (links get read aloud), served at `/g/{slug}`; every per-gallery API route accepts the slug or the uuid. Locally that link works on your machine (or your LAN with `--host 0.0.0.0`); it becomes a real shareable URL the moment the app is hosted.

**Speed, as built.** A selfie search is inference plus almost nothing: each gallery's and collection's embedding matrix and metadata are held in memory, keyed by a version stamp (`indexed_at`/`grouped_at` + face count) so any re-index or regroup invalidates them — the SQLite read that used to cost ~150 ms per search on 11k vectors is paid once. Responses are gzipped (the 800-person wall is ~20 KB on the wire). Face crops are content-addressed and served `immutable` for a year, so a returning visitor's wall is served from cache. Photo tiles get a `thumb_url` (~800 px via the host rewrite rule, else the grid's own thumbnail) instead of the 1600 px indexed source — a 234-photo person view drops from ~100 MB to ~30 MB, and only what scrolls into view loads.

**Persistence.** The local profile has no expiry: `expires_at` is NULL and there is no sweeper, so an index lives until the user deletes it (production keeps its 30-day TTL per PRD D13 — that is a hosted-service concern, not a local one). Jobs are resumable: the manifest is written to `images` before any processing, each image is marked `done`/`failed` as it completes, and a job restarted for any reason processes only what is still `pending`. On startup the server resumes any job the previous process left mid-flight, and re-submitting a failed URL resumes it rather than wiping it.

**Deliberately not built yet:** rate limits, denylist, cost ceiling, TTL sweeper, per-selection share links — all P3, all meaningless for a single-user local process. The privacy *architecture* is already in place: no selfie is ever written (the embedding lives only inside the search request), clusters are anonymous, and `DELETE /api/galleries/{id}` removes the index and its crops.

---

## 16. Collections (pooling galleries)

A collection is a named, ordered set of indexed galleries presented as **one set of people** at `/c/{slug}`. It exists for the common case where one event is published as several galleries — a reception, the portraits, the keynote — and a person wants to search all of them at once.

**What it is not.** A collection never indexes. It holds no faces of its own: `collection_faces` maps existing `faces` rows to `collection_clusters`, and a source can be added or removed without touching any gallery. Deleting a gallery cascades out of every collection it was in.

**Grouping.** On create, and whenever a source changes, the pooled embeddings of every source are re-clustered with the *same* algorithm and thresholds as a single gallery (§5). A person present in three galleries therefore becomes one tile. Cost is a few seconds for ~5k faces — the reason regrouping can be casual.

**Lead thumbnail — "sharpest face wins, any source."** Each pooled person's tile uses the face with the highest `0.4·det_score + 0.3·min(1, short_edge/160) + 0.3·min(1, blur_var/200)`. Each term is capped so a large but soft candid can't outscore a clean portrait; in practice the portrait galleries win almost every tile, which is the intent, without hard-coding a source preference.

**Results carry their source.** Every photo in a person view or a selfie result carries the gallery it came from (`source_slug`, `source_title`) — shown in the tile's hover band and kept as data on the card for later filtering, while the grid itself stays one flat matrix. The collection page itself lists no sources and links nowhere else in the app; sources are managed and visible on `/admin`.

**First collection, FIX-1 + FIX-2 pooled (4,652 faces), default thresholds:** 373 people, of whom **106 appear in both galleries** (461 per-gallery clusters → 373 pooled). Lead thumbnails: 246 from the portrait gallery, 127 from the candid one — and among the 106 cross-gallery people, **102 lead with a portrait**. The largest pooled person (234 photos) is the 142-photo reception person and the 90-photo portrait outlier combined: a host, and a useful sanity check that the two galleries' clusters line up. Regrouping takes ~3 s.

**Third and fourth sources, "Day 1 everything" (2,084 images, 29.6 min, 4,201 usable / 4,607 excluded, 386 people) and "Day 2 everything" (1,637 images, 25.6 min, 2,693 usable / 4,336 excluded, 311 people), added without re-indexing anything.** Pool: **11,000 faces → 794 people**; 280 people appear in 2+ galleries and 30 in all four. The largest pooled person now spans 574 photos across all four sources — consistent with the host reading above. Each regroup of the 11k-face pool took a few seconds; the two ~2k-image galleries ran concurrently in one process, serialized on the inference lock, at ~0.85 s/image effective.

**Privacy posture is unchanged.** Pooling widens what one selfie can be matched against, which is exactly why a collection is an explicit, named, user-created thing rather than a default — nothing is ever searched across galleries that nobody grouped.

---

## 17. Hosting profile

The hosted deployment is the local profile minus indexing: FastAPI + the models + SQLite + crops on a persistent volume. It exists to serve one standalone collection page and its selfie search to people on phones; everything heavy stays on the operator's machine.

| | Local | Hosted |
|---|---|---|
| Indexing | on | **off** (`FACES_ALLOW_INDEX=1` to enable; not recommended — Chromium, long CPU jobs) |
| Auth | none | `FACES_ADMIN_TOKEN`: Bearer on every write route and the admin listings; `/admin?token=…` stores it in that browser |
| Data | `./data` | `/data` volume: `faces.db`, `crops/`, `models/` (downloaded on first start) |
| Getting an index there | — | `publish.sh` → `POST /api/admin/import` with a tarball of `faces.db` + `crops/`; validated (no path escapes or links, must open, must have the expected tables), then swapped in under the inference lock; previous index kept as `data.prev` |
| Public writes | — | only selfie search, per-IP token bucket (20/min, burst 8), `Retry-After` on 429 |
| Image | — | `python:3.12-slim`, ~1 GB with models cached on the volume; needs ~1.5 GB RAM for the model |

**Why publish-from-local instead of index-on-server.** Indexing needs a headless browser and 15–30 minutes of CPU per gallery; a shared container is the wrong place for it, and it would put the crawl's egress on the host's IP. Publishing a finished index is a 150 MB upload that takes a minute.

**Why the bundle is never in the repo or a release.** `faces.db` holds the embeddings — biometric identifiers under GDPR Art. 9 / BIPA. They travel operator → server over HTTPS behind the admin token and nowhere else. Source photos are never copied anywhere; the page hotlinks them.

**Image notes.** `insightface` builds from source on Linux (hence `build-essential` in the build stage) and drags in the full `opencv-python` beside the headless one; that `cv2` links X11/GL at import, so the runtime stage installs `libgl1 libglib2.0-0 libxcb1 libx11-6 libxext6 libsm6 libxrender1` (first deploy crashed on `libxcb.so.1`). `libgomp1` is for ONNX Runtime. The image runs an import smoke test at build time so a missing library fails the build instead of crash-looping the container. If the platform's memory ceiling is under ~1.5 GB the model won't load — the fallback is `buffalo_s` with a re-index.
