# Faces — Product Requirements Document

| | |
|---|---|
| **Status** | Draft v0.1 — pre-build |
| **Owner** | Oz Nakash |
| **Last updated** | 2026-09-20 |
| **Benchmark workload** | A 1,000-image public gallery (all speed and cost figures normalize to this; test fixtures in §7) |
| **Related** | [Technical Spec](./TECH-SPEC.md) · [Executive Summary](./EXEC-SUMMARY.md) |

---

## 1. Why

### The problem

Photo galleries are published as a flat wall of images. A conference, a wedding, a marathon, a school year, a community event — any of them routinely produces **one to several thousand photos in a single gallery**. Someone who wants their own photos has exactly one tool: scroll, squint, and hope. At roughly 1.5 seconds per thumbnail, a 1,200-photo gallery is **~30 minutes of manual scanning** for a handful of hits, and most people give up long before the end.

The photos exist, they are public, and the person in them will never see them. That is the gap — and it is the same gap on every gallery platform, which is why this is a product and not a script.

### Why now

1. **Face recognition is commoditized.** Open-weight models (InsightFace / ArcFace) hit >99.5% accuracy on standard benchmarks and run at tens of images per second on a single small GPU. What required a research team in 2018 is a weekend of plumbing in 2026.
2. **Cost has collapsed.** Indexing 1,000 photos costs on the order of **$0.03–$1.00** depending on the engine (see Tech Spec §9). That is an affordable unit economic for a free consumer tool.
3. **Nobody has done it for the open web.** Google Photos and Apple Photos do this beautifully *inside your own library*. Photographer platforms (SmugMug, Zenfolio, Pixieset) do it for the *photographer's* paid clients. Nothing lets a third party point at a public gallery URL and ask "where am I?"

### Why us / why this is defensible enough to be worth building

Not a moat play — a **wedge**. The differentiated asset is the *gallery adapter layer*: the boring, unglamorous work of turning an arbitrary public gallery URL into a clean, complete, rights-respecting image manifest. Recognition is a commodity; reliable ingestion across SmugMug, Zenfolio, Flickr, Pixieset, WordPress and plain HTML is not. Every adapter added compounds.

### What we believe (and are testing)

| # | Hypothesis | How v1 tests it |
|---|---|---|
| H1 | People will paste a gallery URL to find themselves | Selfie-search completion rate ≥ 60% of sessions that start one |
| H2 | A "wall of faces" is a more compelling entry point than a search box | Face-wall click-through ≥ 40% of gallery views |
| H3 | Accuracy at event-photo quality (motion, low light, profiles) is good enough to trust | ≥ 90% recall at ≥ 98% precision on the labeled eval set |
| H4 | Event organizers and photographers will want this as a service | ≥ 3 inbound requests from organizers/photographers within 60 days of launch |

### Non-goals (explicitly out of scope for v1)

- **Naming people.** No identity resolution, no name lookup, no social-media matching, no celebrity recognition. Clusters are anonymous ("Person 7") unless a user names their own cluster locally.
- Video, live streams, or non-photo media.
- Password-protected, paywalled, or login-gated galleries.
- A mobile app (responsive web only).
- Photo editing, purchase, or print-ordering flows.
- Anything that builds a **persistent cross-gallery face database**. Indexes are per-gallery and expire. This is a product decision *and* a legal one (§7).

---

## 2. What

### One-line definition

**Faces turns any public photo gallery URL into a people-indexed gallery: see everyone who is in it, and find every photo you are in — from a selfie or a click.**

### The two v1 flows

**Flow A — Browse by face (the "face wall")**
1. User pastes a gallery URL.
2. Faces ingests the gallery, detects every face, and groups them into unique people.
3. User sees a grid of face thumbnails — one per person found.
4. Clicking a face shows every photo that person appears in, with a link back to the original gallery page for each photo.

**Flow B — Find me (selfie search)**
1. User pastes a gallery URL and uploads or captures a selfie.
2. Faces returns every photo in that gallery containing that face, ranked by confidence.
3. The selfie and its embedding are discarded when the session ends (default TTL 30 minutes).

### Principles

1. **Zero setup.** No account, no install, no API key from the user. A URL is the entire input.
2. **Show progress, never a spinner.** A thousand-image gallery takes minutes. Faces stream in as they are found; the first faces appear in seconds, not at the end.
3. **Anonymous by default.** We recognize *that* two faces are the same person. We never claim *who* they are.
4. **Link home, don't replace.** Every result deep-links to the photo on the original gallery. We are a lens on someone else's gallery, not a mirror of it.
5. **Honest confidence.** Borderline matches are shown as "possible matches" in a separate tray, not silently mixed with confident ones.

### Scope boundary for v1

| In | Out (v1.5+) |
|---|---|
| SmugMug adapter + generic HTML adapter | Zenfolio, Pixieset, Flickr, Google Photos share links |
| Face wall + selfie search | Naming / labeling clusters, saved people |
| Single-gallery indexes with TTL | Cross-gallery search, persistent user library |
| Download / share selected photos | Bulk ZIP export, email delivery |
| Public galleries | Auth-gated galleries, organizer dashboards |

---

## 3. How

### Approach in one paragraph

A URL goes to an **adapter** that resolves it to a complete image manifest — for SmugMug that means extracting the album key from the page and paginating the sanctioned `/api/v2` endpoint rather than scraping (the site's `robots.txt` disallows generic crawling but explicitly allows `/api/v2`). Images are fetched at display resolution, deduplicated by content hash, and run through a two-stage model: **SCRFD** for detection and **ArcFace** for a 512-dimension embedding per face. Embeddings go into Postgres with `pgvector`. Faces are grouped into people by graph clustering over cosine similarity. Selfie search is a single ANN lookup against that same index. Results stream to the browser over SSE as the job progresses. Full design in the [Technical Spec](./TECH-SPEC.md).

### Delivery plan

| Phase | Goal | Exit criteria |
|---|---|---|
| **P0 — Spike** (week 1) | Prove the pipeline end-to-end on one real gallery | 200 images from a fixture gallery indexed; face wall renders; measured recall/precision on 50 labeled photos |
| **P1 — Face wall** (weeks 2–3) | Flow A, production-shaped | Every fixture gallery indexed end-to-end within SLA; streaming progress; deep links work |
| **P2 — Selfie search** (week 4) | Flow B | Selfie → ranked results in < 3s against a warm index; selfie TTL enforced |
| **P3 — Share & harden** (weeks 5–6) | Make it shareable and safe | Download/share, rate limits, takedown path, privacy notice, eval harness in CI |

### Key decisions taken (with the alternative rejected)

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Recognition engine | Self-hosted InsightFace (SCRFD + ArcFace) | AWS Rekognition Collections | ~35× cheaper at volume, no per-image vendor lock, embeddings stay ours; Rekognition kept as a documented fallback |
| Clustering | Graph / connected-components over cosine threshold + centroid merge | k-means, HDBSCAN | Number of people is unknown a priori; graph approach is incremental and explainable |
| Ingestion | Per-platform adapters, API-first | Universal headless-browser scraper | Correctness, completeness, and robots compliance; headless browser is the fallback, not the default |
| Identity | Anonymous clusters only | Name enrichment | Legal exposure (BIPA/GDPR Art. 9) is disproportionate to v1 value |
| Storage of selfies | In-memory, TTL 30 min, never written to disk | Persist for "improved results" | Trust is the product's fragile asset |

---

## 4. Jobs To Be Done

Four jobs, in priority order. Each is scoped to v1 unless marked.

### JTBD-1 — Find me *(primary)*

> **When** I've attended an event and the photographer posts a thousand-photo gallery,
> **I want to** find every photo I appear in without scrolling the whole gallery,
> **so I can** save, share, and use the photos of myself that already exist.

- **Current alternative:** manual scroll (~30 min), or ask a friend to spot you.
- **Success signal:** user reaches a result set and downloads or opens at least one photo.
- **Hired when:** time-to-first-correct-photo is under 2 minutes from pasting the URL.
- **Fired when:** results miss photos the user knows they are in, or include strangers.

### JTBD-2 — Find the people I care about

> **When** I'm looking through an event gallery,
> **I want to** see who is in it and jump straight to one person's photos,
> **so I can** find shots of my teammate, my speaker, or the person I met at the reception.

- **Current alternative:** none. Galleries have no people index at all.
- **Success signal:** face-wall click-through, then ≥ 2 people opened per session.
- **Hired when:** the face wall is legible — distinct, recognizable crops, most-photographed people first.
- **Fired when:** the wall is full of duplicates of the same person, or unrecognizable blurry crops.

### JTBD-3 — Send people their photos *(organizer / photographer; v1 read-only, v1.5 full)*

> **When** I've run an event or shot it,
> **I want** a people index of my gallery,
> **so I can** deliver each attendee their photos instead of sending everyone a link to the whole gallery.

- **Current alternative:** platform-native face tagging (SmugMug, Pixieset) — photographer-side only, paid tiers, and locked to one platform.
- **v1 scope:** the organizer can use the same face wall and share a per-person link. Bulk delivery is v1.5.
- **Hired when:** a per-person link is stable and shareable.
- **Fired when:** the index expires before the organizer finishes using it.

### JTBD-4 — Get the photos out

> **When** I've found my photos,
> **I want to** download or share them in one action,
> **so I can** actually use them without right-clicking through twenty tabs.

- **Current alternative:** manual save-as, one at a time.
- **v1 scope:** select-and-download, plus a shareable result link. Respects the source gallery's download permissions — where the gallery blocks downloads, we link out instead.
- **Hired when:** selection → download is one click and preserves original resolution where permitted.
- **Fired when:** we serve images the gallery owner has marked as not downloadable.

---

## 5. What "done" is

v1 ships when **every** item below is true. These are binary, not aspirational.

### Functional

- [ ] **D1** Pasting a supported gallery URL produces a **complete** index: where the adapter can know the true total, the indexed count matches it exactly; where it cannot, that is stated in the UI. Skipped images are always counted and surfaced, never silent.
- [ ] **D2** The face wall renders one representative crop per detected person, sorted by photo count descending.
- [ ] **D3** Clicking a person shows all photos containing them, each deep-linking to its page on the source gallery.
- [ ] **D4** Uploading a selfie against an indexed gallery returns a ranked result set, split into "confident" and "possible" trays.
- [ ] **D5** Results stream during indexing — first faces visible before the job completes.
- [ ] **D6** Re-submitting an already-indexed URL returns cached results without re-processing.
- [ ] **D7** Selected photos can be downloaded or shared via a stable link.

### Quality gates

- [ ] **D8** On the labeled eval set (§7 of Tech Spec): **recall ≥ 90%** at **precision ≥ 98%** for selfie search.
- [ ] **D9** Cluster purity ≥ 0.90 and no person split across more than 2 clusters on the eval set.
- [ ] **D10** A 1,000-image gallery indexed in **≤ 7 minutes** (and any fixture gallery in ≤ 12); first faces visible in **≤ 60 seconds**; selfie query against a warm index in **≤ 3 seconds** (p95).
- [ ] **D11** All test cases in §7 pass on **every** fixture gallery — not just the one it was tuned on — and the eval harness runs in CI on every change to the model or threshold config.

### Safety, privacy, and legal

- [ ] **D12** Selfies and their embeddings are never persisted to disk and are purged at session end or 30 minutes, whichever is first.
- [ ] **D13** Gallery indexes expire automatically (default 30 days) and there is a working one-click delete.
- [ ] **D14** A privacy notice explains, in plain language, what is processed, where it goes, and how long it lives — shown before the first upload, not buried.
- [ ] **D15** A takedown path exists for gallery owners and for individuals, with a documented SLA.
- [ ] **D16** Ingestion respects `robots.txt` and platform ToS; the SmugMug path uses the sanctioned `/api/v2` interface.
- [ ] **D17** Rate limiting and an abuse policy are live: per-IP and per-domain caps, and a denylist for domains we will not index.

### Operational

- [ ] **D18** Indexing cost per 1,000 images is measured and recorded, and a hard per-job cost ceiling aborts runaway jobs.
- [ ] **D19** Failed jobs surface a specific, actionable reason to the user (not "something went wrong").
- [ ] **D20** README, PRD, and Tech Spec are current with what actually shipped.

---

## 6. What "good" looks like

"Done" is the floor. This is the bar.

### Good is *fast enough to feel free*

The wait is the product's biggest risk. Good means the first faces appear in **under 15 seconds**, the wall is usable while the rest streams in, and a warm selfie query feels instant (**< 1 second**). Nobody watches a progress bar for eight minutes.

### Good is *trustworthy at the edges*

Any system can match a well-lit frontal portrait. Event photography is the hard case: motion blur, backlit stages, profiles, people half-behind other people. Good means:
- **We never confidently show a stranger.** A false positive costs more trust than three false negatives cost value. Precision is the protected metric.
- Low-confidence matches live in a visually distinct "possible matches" tray, labeled as such.
- When we are unsure, we say so — "12 confident, 4 possible" — rather than presenting one undifferentiated grid.

### Good is *legible*

A user should be able to tell, without instructions, what a face crop is, why a photo matched, and where the original lives. Each result shows its source photo, its match confidence, and a link home.

### Good is *self-evidently respectful*

Someone who did not ask to be indexed should, on reading the privacy notice, conclude that we made careful choices: anonymous clusters, expiring indexes, no cross-gallery database, no name enrichment, real deletion. The policy should read like a product decision, not a legal shield.

### Good is *boring to operate*

One job type, one queue, one database. Retries are automatic and idempotent. A gallery that fails halfway resumes rather than restarting. Cost per gallery is known to the cent and capped.

### Counter-metrics we watch

| Metric | Direction | Why |
|---|---|---|
| False-positive rate | ↓ protected | A stranger in your results destroys trust instantly |
| Takedown / deletion requests | ↓ | Rising volume means we've misjudged consent |
| Jobs aborted on cost ceiling | ↓ | Indicates broken ingestion, not demand |
| p95 time-to-first-face | ↓ | The only latency number users actually feel |
| Clusters per real person | → 1.0 | Over-splitting is the most common silent quality failure |

---

## 7. Test cases

Format: **ID · Scenario · Expected**. All cases are automatable except where marked *(manual)*.

### Fixtures

The fixture set exists to stop us from building a tool that works on exactly one gallery. It deliberately spans different **shapes** of gallery, not just different URLs — and must include at least one non-SmugMug source before P1 exits.

| ID | Gallery | Shape | Why it's in the set |
|---|---|---|---|
| **FIX-1** | Candid event gallery — [IIA AI Summit, Opening Reception](https://www.johnwernerphotography.com/IIA-AI-Summit-Silicon-Valley-Sept-13-15-2026/Opening-Reception) (SmugMug, ~1,170 images) | Crowded frames, mixed and low light, motion, many faces per photo, lots of profiles | The hard case. Detection recall and cluster fragmentation are decided here |
| **FIX-2** | Portrait gallery — [IIA AI Summit, All the portraits](https://www.johnwernerphotography.com/IIA-AI-Summit-Silicon-Valley-Sept-13-15-2026/All-the-portraits) (SmugMug, ~1,830 images) | Posed, frontal, even lighting, one or two subjects per frame | The easy case, and therefore the clean signal: a precision failure here is unambiguous. Also the largest fixture — the scale test |
| **FIX-3** | A non-SmugMug gallery — generic HTML or JSON-LD source *(to be selected in P0)* | Unknown total, lazy loading, no sanctioned API | Proves the adapter layer is not SmugMug-shaped. **Blocks P1 exit** |
| **FIX-4** | A small non-gallery page (e.g. an article with 3 photos) | Boundary | Proves graceful behavior outside the happy path |

FIX-1 and FIX-2 are the same event, which makes them a useful pair: the same people appear in both, under very different conditions, so a person's embedding quality can be compared across shapes.

### A. Ingestion

| ID | Scenario | Expected |
|---|---|---|
| T-A1 | Submit FIX-1 URL | Every image enumerated; count matches the source's own reported total exactly; zero silent skips |
| T-A2 | Submit the same URL a second time | Cache hit; results returned in < 3s; no re-fetch of source images |
| T-A3 | Submit a URL with no images | Clear message: "No images found at this URL" — not an error page |
| T-A4 | Submit a password-protected / paywalled gallery | Detected and refused with a specific reason; no partial index created |
| T-A5 | Submit a non-gallery URL (news article with 3 photos) | Generic adapter handles it; 3 images indexed or an honest "too few images" message |
| T-A6 | Submit a malformed URL / non-existent domain | Validation error in < 2s, no job created |
| T-A7 | Source host returns 429/503 mid-crawl | Exponential backoff, job resumes, no duplicate work, no image lost |
| T-A8 | Gallery contains the same photo twice at different URLs | Deduplicated by content hash; counted once |
| T-A9 | Domain whose `robots.txt` disallows our path and offers no sanctioned API | Refused with an explanation; logged; not retried |
| T-A10 | Job exceeds the per-job cost ceiling | Aborted cleanly; partial results retained and labeled partial |
| T-A11 | Submit FIX-3 (non-SmugMug, no sanctioned API, unknown total) | Indexed via the structured-data or HTML adapter; UI states that completeness is unverified rather than implying a full count |

### B. Detection & clustering

| ID | Scenario | Expected |
|---|---|---|
| T-B1 | Group photo with 25+ faces | ≥ 90% of human-countable faces detected |
| T-B2 | Photo with faces smaller than the quality threshold (background crowd) | Excluded from the wall; not counted as a miss |
| T-B3 | Same person across 3 lighting conditions (stage, hallway, outdoor) | Single cluster, not three |
| T-B4 | Same person with and without glasses / with and without a badge lanyard | Single cluster |
| T-B5 | Person photographed only in profile | Detected; either clustered correctly or left as a singleton — never merged into a different person |
| T-B6 | Back-of-head / fully occluded face | Not detected; no phantom cluster |
| T-B7 | Two different people with similar appearance | Two clusters; no merge |
| T-B8 | Photo of a poster/screen showing a face | Either excluded by quality filter or clustered separately; documented behavior *(manual review)* |
| T-B9 | Cluster count on FIX-1 | Within ±15% of a human count of distinct attendees on a 100-photo sample *(manual)* |
| T-B10 | Re-run clustering with the same inputs | Deterministic — identical cluster assignment |

### C. Selfie search

| ID | Scenario | Expected |
|---|---|---|
| T-C1 | Selfie of a person known to be in 12 FIX-1 photos | ≥ 11 of 12 returned as confident; zero strangers in the confident tray |
| T-C2 | Selfie of a person **not** in the gallery | Empty confident tray with an explicit "no matches" state — never a nearest-neighbor fallback |
| T-C3 | Selfie containing two faces | User is asked which face to search, or the largest/most central is used with a visible, changeable indicator |
| T-C4 | Selfie with no detectable face | Specific error: "We couldn't find a face in that photo" + retake guidance |
| T-C5 | Very low-resolution or heavily blurred selfie | Warned before searching; results labeled lower-confidence |
| T-C6 | Upload of a non-image file, or a 50 MB image | Rejected at the boundary with a clear size/type message |
| T-C7 | Selfie query against a gallery still indexing | Runs against the indexed subset, with a visible "still indexing — N of M" state and auto-refresh |
| T-C8 | Same selfie submitted twice | Identical ranked results |
| T-C9 | p95 latency, warm index, 1,000+ image gallery | ≤ 3s end-to-end |
| T-C10 | Same selfie run against FIX-1 (candid) and FIX-2 (portraits) | Both return the same person; recall on FIX-2 ≥ recall on FIX-1. A *lower* score on the posed gallery means preprocessing is broken, not that the photos are hard |

### D. Privacy, safety, and abuse

| ID | Scenario | Expected |
|---|---|---|
| T-D1 | Selfie retention | No selfie bytes or embeddings on disk at any point; purged ≤ 30 min after session end; verified by inspection |
| T-D2 | Gallery index TTL | Index and all derived crops deleted at TTL; verified by a scheduled job test |
| T-D3 | User clicks "delete this index" | Index, crops, and embeddings gone within 60s; subsequent lookup returns not-found |
| T-D4 | Domain on the denylist | Refused before any fetch |
| T-D5 | 50 job submissions from one IP in a minute | Rate limited with a clear retry-after; no queue starvation for other users |
| T-D6 | Takedown request received | Documented path executes; index removed; action logged *(manual)* |
| T-D7 | Privacy notice | Shown before the first selfie upload; states what is processed, retention, and deletion in plain language *(manual)* |

### E. Experience

| ID | Scenario | Expected |
|---|---|---|
| T-E1 | Time to first face, FIX-1 | ≤ 15s target, ≤ 60s hard limit |
| T-E2 | Full index, per 1,000 images | ≤ 7 min |
| T-E3 | Browser closed and reopened mid-job | Job continues server-side; returning to the URL shows current progress |
| T-E4 | Face wall on a 375px-wide mobile viewport | Usable; crops legible; no horizontal scroll |
| T-E5 | Every result photo | Deep-links to its page on the source gallery and opens correctly |
| T-E6 | Job fails for any reason | Specific, actionable message; never a bare "something went wrong" |
| T-E7 | Keyboard and screen-reader navigation of the face wall | Focusable, labeled, operable *(manual)* |

---

## 8. Open questions

| # | Question | Needed by | Current lean |
|---|---|---|---|
| Q1 | Do we require gallery-owner consent before indexing, or operate on a takedown basis? | P3 | Takedown basis for public galleries; revisit if friction appears |
| Q2 | Do we geo-gate Illinois and Texas (BIPA / CUBI biometric statutes)? | P3 | Seek counsel; default to gating rather than risking it |
| Q3 | Is the face wall or the selfie box the default landing experience? | P1 | Face wall — it works with zero user input and demos better |
| Q4 | Does a user get to name their own cluster locally (device-only)? | v1.5 | Yes, device-local only, never server-side |
| Q5 | What is the business model, if any? | Post-launch | Free consumer tool; organizer/photographer tier is the candidate (validates H4) |
