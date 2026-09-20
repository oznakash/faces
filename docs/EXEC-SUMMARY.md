# Faces — Executive Summary

*Two-minute read. Full detail in the [PRD](./PRD.md) and [Technical Spec](./TECH-SPEC.md).*

**Status:** pre-build, docs complete · **Owner:** Oz Nakash · **Updated:** 2026-09-20

---

## The problem

A single event gallery routinely holds **one to several thousand photos**. A conference, a wedding, a marathon, a school year — the pattern is identical. If you're in there, the only way to find your photos today is to scroll the whole gallery and squint: roughly **30 minutes of manual scanning** per thousand images, for a handful of hits. Most people never try.

The photos exist, they're public, and the person in them will never see them. It's the same gap on every gallery platform — which is what makes this a product rather than a script.

## The product

**Faces turns any public gallery URL into a people-indexed gallery.**

- **Browse by face** — paste a URL, get a wall of every distinct person in the gallery, click one to see all their photos.
- **Find me** — paste a URL, add a selfie, get every photo you're in, ranked and split into *confident* and *possible*.

Zero setup: no account, no install, no API key from the user. A URL is the entire input.

## Why now

Face recognition is commoditized (open ArcFace weights, >99.5% benchmark accuracy), and indexing 1,000 photos costs **~3 cents**. Google Photos does this inside *your* library; SmugMug does it for a photographer's *paying clients*. Nothing lets a third party point at a public gallery and ask "where am I?"

The durable asset isn't the recognition — that's a commodity. It's the **gallery adapter layer**: reliably and legitimately turning an arbitrary gallery URL into a complete image manifest. Every platform adapter compounds.

## The four jobs

1. **Find me** *(primary)* — find every photo I'm in without scrolling the whole gallery.
2. **Find the people I care about** — see who's in the gallery and jump to one person's photos.
3. **Send people their photos** *(organizer/photographer)* — a people index so attendees get their own shots, not a link to the whole gallery.
4. **Get the photos out** — download or share the results in one action.

## What we will not build

No names. No cross-gallery face database. No stored selfies. No scraping what `robots.txt` forbids. These are enforced structurally — **there is no schema in which identities could accumulate** — not promised in a policy page. It's the right call ethically, it's the right call legally (GDPR Art. 9, Illinois BIPA, Texas CUBI), and it's the only version of this product that deserves to be trusted.

## Done vs. good

**Done** is 20 binary gates across function, quality, privacy, and operations, validated by **45 test cases** run across a *set* of fixture galleries. The load-bearing ones:

| | Gate |
|---|---|
| Accuracy | **≥ 90% recall at ≥ 98% precision**; cluster purity ≥ 0.90 |
| Speed | First faces **≤ 15s** · **≤ 7 min per 1,000 images** · selfie query **≤ 3s** |
| Privacy | Selfies never touch disk, 30-min TTL · indexes expire in 30 days · working delete |

**Good** goes past the gates on one axis above all: **precision is the protected metric.** One stranger in your results costs more trust than three missed photos cost value — so borderline matches go in a visibly separate "possible" tray rather than being quietly mixed in. Good also means fast enough to feel free (results stream; nobody watches an 8-minute progress bar) and boring to operate (one job type, one queue, resumable, cost capped to the cent).

## How it's built

```
URL → adapter → manifest → fetch+dedupe → SCRFD detect → ArcFace embed (512-d)
                                                    ↓
                        face wall ← graph cluster ← pgvector HNSW → selfie ANN search
```

Next.js app (UI, SSE progress) + Python worker (InsightFace on ONNXRuntime) + Postgres/pgvector + Redis + R2. Two services, one database.

**The decision that matters most:** self-hosted InsightFace over AWS Rekognition — **~35× cheaper** ($0.03 vs $1.00 per 1,000 images), no vendor lock, and embeddings stay ours. Rekognition is kept as a documented failover behind the same interface.

**Per 1,000 images:** ~2 min and ~$0.03 on a small GPU; ~6.5 min and ~$0.04 CPU-only. Both clear the 7-minute gate; the CPU path clears it with little margin, which is why fetch and inference overlap — and why larger galleries need the GPU path.

**The real engineering risk isn't the models, it's ingestion.** Gallery pages are JS-rendered: a naive fetch returns a handful of image URLs out of a thousand *and looks like it worked*. So every adapter must either verify its count against a source-reported total or openly declare completeness unverified. Silent truncation is the most dangerous failure mode in the system.

## Plan

| Phase | Weeks | Deliverable |
|---|---|---|
| **P0** | 1 | First platform adapter, detect→embed→store on 200 images, **labeled eval set + harness** |
| **P1** | 2–3 | Clustering, face wall, streaming progress, full runs across **every fixture** — including a non-SmugMug gallery |
| **P2** | 4 | Selfie search, two-tier results, TTL enforcement |
| **P3** | 5–6 | Share/download, rate limits, privacy notice, takedown path |

**P0 is not a throwaway spike.** It carries the eval set — the artifact every later accuracy decision is judged against. Build it first or tune blind. And the fixtures are not decoration: a system tuned against one gallery is a demo, so a non-SmugMug fixture gates P1's exit.

## Top risks

| Risk | Mitigation |
|---|---|
| **Precision below 98%** on event-quality photos (motion, backlight, profiles) | Two-tier thresholds; precision as a hard constraint in calibration; eval gate in CI |
| **Over-split clusters** make the face wall noisy — the most likely silent quality failure | Centroid consolidation pass; fragmentation is an explicit eval gate (≤ 2.0 clusters/person) |
| **Biometric-privacy exposure** (BIPA/CUBI) | Anonymous clusters, TTLs, no persistence — plus counsel before public launch |
| **Misuse for stalking** | No naming, no cross-gallery search, rate limits, denylist, takedown path |
| **A platform's sanctioned API needs credentials we can't get** — that loses a whole category of galleries, not one | Secure each adapter's access path before it ships; never depend on a single platform; headless fallback only with an explicit robots decision |

## Three decisions needed before P3

1. **Consent or takedown?** Do we index public galleries on a takedown basis, or seek gallery-owner consent first? *(Lean: takedown for public galleries.)*
2. **Geo-gate Illinois and Texas?** BIPA carries a private right of action. *(Lean: gate rather than risk it — needs counsel.)*
3. **Is there a business?** Free consumer tool, with an organizer/photographer tier as the candidate. Tested by whether organizers come asking.

## Next step

Start **P0**: secure the first adapter's API access, select the non-SmugMug fixture, and build the labeled eval set before anything else. Fixture galleries are listed in [PRD §7](./PRD.md#fixtures).
