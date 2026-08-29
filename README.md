# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns structured JSON. Built directly on
LinkedIn's internal Voyager and RSC endpoints over raw HTTP — no browser, no
automation driver, no headless Chrome.

```
GET /v1/profile?url=https://www.linkedin.com/in/samyakj05/
X-API-Key: <key>
```

**Live:** Set after deployment · **Docs:** `<deployment URL>/docs` ·
**Health:** `<deployment URL>/v1/health`

---

## Contents

- [Approach](#approach) — what I built and why
- [How Voyager works](#how-voyager-works) — the reverse-engineering
- [Setup](#setup)
- [API reference](#api-reference)
- [Testing](#testing)
- [Known limitations](#known-limitations) — read this one
- [Legal and data protection](#legal-and-data-protection)

---

## Approach

The interesting problem here is not fetching a profile once. It is building
something that still works next week.

Voyager is undocumented, versioned, and actively defended. Its GraphQL
queries are keyed by hashes that rotate on every relevant frontend deploy,
its responses are a render tree rather than a data model, and its dominant
failure mode is a `200 OK` carrying degraded data rather than an error. Any
of those will silently break a naive client, and the breakage looks like
success.

So three decisions shape everything else.

**1. Tiered fetching, with fallback.** The current RSC experience card is
the primary detailed path. Dash REST resolves the profile identity, an
optional Voyager GraphQL route remains a compatibility path for additional
sections, and the public page provides a thin fallback.

| Tier | Path | Auth | Breaks when |
|---|---|---|---|
| 1 | RSC profile card | full browser cookie context | UI request shape changes or session is challenged |
| 2 | Voyager dash REST | session cookie | the session expires or the IP is blocked |
| 3 | Voyager GraphQL | session cookie + current `queryId` | the `queryId` rotates |
| 4 | Public page JSON-LD | none | LinkedIn serves an auth wall |

Tier 1 is the primary path. The others exist because their failure modes
are genuinely independent — a stale hash kills tier 1 while tier 2 keeps
working; a datacentre IP block kills both while tier 3 keeps working. All
three hit LinkedIn endpoints directly; none involves a browser.

**2. Provenance travels with the data.** Every response records which tier
answered each section, under which `queryId`, verified on what date. This
does double duty: operationally it is how endpoint rot is detected, and
structurally it is what makes source-scoped deletion possible. More on the
second point under [Legal](#legal-and-data-protection).

**3. Completeness is a first-class field, not an inference.** Responses are
classified `complete`, `partial`, or `needs_review`. That third state is the
important one — it means the fetch succeeded, the parse succeeded, and the
result still looks wrong. A profile with a name but no experience is
possible, but far more often means the extraction broke.

```
GET /v1/profile
  │
  ├─ extract slug ................. URL, locale subdomain, deep link, or bare slug
  ├─ cache ........................ short TTL, purgeable
  ├─ authenticated tiers
  │    ├─ resolve slug → fsd_profile URN ..... dash REST
  │    ├─ experience card .................... RSC/SDUI stream
  │    └─ optional section fetch ............. GraphQL, queryId registry
  ├─ public fallback ............... fills gaps, or stands alone
  ├─ parse ........................ URN graph + component tree
  ├─ merge ........................ richer tier wins; thinner tier fills gaps
  └─ assess ....................... completeness + warnings
```

---

## How Voyager works

The parts worth knowing, since this is the substance of the exercise.

### Authentication

Two cookies. `li_at` carries the session; `JSESSIONID` looks like
`"ajax:1234567890123456789"` — **including literal double quotes**.

The CSRF scheme is a double-submit cookie: the `csrf-token` header must
equal the `JSESSIONID` value with the quotes stripped. This is less naive
than it looks. A cross-origin attacker can make a browser *send* your
cookies but the same-origin policy stops them *reading* those cookies, so
they cannot populate a matching header. It is stateless and sound — in a
browser. It offers nothing here because we hold the cookie directly.

Three headers are load-bearing in non-obvious ways:

| Header | Why |
|---|---|
| `accept: application/vnd.linkedin.normalized+json+2.1` | Selects the flat `{data, included[]}` format. Without it you get a deeply nested structure that is far worse to parse. |
| `x-restli-protocol-version: 2.0.0` | Changes the response encoding entirely. Its absence produces confusing shapes rather than clean errors. |
| `user-agent` | A non-browser UA draws an immediate `HTTP 999 Request denied`, before any application logic runs. |

There is deliberately **no login flow**. LinkedIn's authentication gates on
CAPTCHA, 2FA and checkpoint challenges that cannot be cleared from a raw
HTTP client. The session is harvested once from a browser and injected
through the environment, which is what production systems in this space
actually do.

### Three coexisting generations

Most writing on Voyager describes two: legacy REST, then GraphQL. There are
at least three generations, whose availability varies by account, route,
experiment, and frontend deployment.

```
Legacy REST   /voyager/api/identity/profiles/{publicId}/profileView
              Returned nearly everything in one call. Largely dead — the
              reason old tutorials look easier than reality.

Dash REST     /voyager/api/identity/dash/profiles?q=memberIdentity&…
              Still the most reliable slug → URN resolver. Tier 2.

GraphQL       /voyager/api/graphql?queryId=…&variables=(…)
              A legacy/current transitional profile-content route. Tier 1,
              and the fragile one.

RSC routes    /flagship-web/…
              The current web UI uses server-component requests. This API
              reconstructs the observed profile-card envelope and calls the
              endpoint directly over HTTP at runtime. The browser is used
              only during development to inspect the request contract.
```

### The Rest.li encoding trap

GraphQL variables are **not JSON**. They are Rest.li 2.0 encoding —
parentheses, colons, percent-encoded URN colons. This is the most common
cause of an opaque 400 on a first attempt:

```
WRONG   variables={"profileUrn":"urn:li:fsd_profile:ACoAAA…"}
RIGHT   variables=(profileUrn:urn%3Ali%3Afsd_profile%3AACoAAA…)
```

### The response is a graph

`included` is a flat array of every entity the query touched, each carrying
an `entityUrn` identity and a `$type` discriminator. Fields prefixed `*` are
references. So parsing means indexing by URN and walking references —
rebuilding a normalized graph the real frontend was expected to reassemble.

Type matching is done on a **suffix** (`identity.profile.Profile`) rather
than the fully-qualified name, because LinkedIn moves classes between
packages across releases and pinning the full path breaks the parser on a
rename that changed nothing about the data.

### The component tree, and the bug it caused

Profile sections arrive as `topComponents` — a generic renderable UI tree of
`textComponent`, `entityComponent`, `insightComponent`. We are parsing
LinkedIn's **render tree**, not a data model. A purely cosmetic redesign
changes our output. That is the structural reason this integration is
fragile, and no amount of careful coding removes it.

A concrete example, because it actually happened during development. A
multi-role employer nests its individual roles inside the outer component's
`subComponents`. My first walk was unpruned, so it found those nested roles
*again* at the top level and emitted them a second time as standalone jobs
— a person with three promotions at one company produced the correct group
plus three phantom positions. The output looked entirely plausible. It was
caught by an explicit count assertion
(`tests/test_components.py::test_only_top_level_components_returned`), not
by inspection.

That case is also why `PositionGroup` exists in the schema. LinkedIn models
"three promotions at one employer" as a group with children, and flattening
it either destroys promotion history or emits unrelated duplicate jobs. It
is the most common defect in profile parsers and it fails silently.

---

## Setup

Requires Python 3.11+.

```bash
git clone <repo> && cd linkedin-profile-api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

### Getting a session

1. Log into LinkedIn in a browser, **with "remember me"**.
2. DevTools → Application → Cookies → `https://www.linkedin.com`.
3. Copy `li_at` and `JSESSIONID` into `.env`. Keep JSESSIONID's quotes —
   config restores them if your shell strips them.

Use a secondary account, not one you depend on. See
[Known limitations](#known-limitations).

If a current route requires the broader authenticated cookie context, set
`LINKEDIN_COOKIE_HEADER` instead. It must include `li_at` and `JSESSIONID`,
is treated as a deployment secret, and is never logged or committed. The
application still sends raw HTTP requests only; it does not run a browser.

### Getting a queryId

`app/linkedin/queries.py` ships with profile-query IDs intentionally empty.
The previously captured component query is not included because it no longer
returns profile data. To populate or refresh one:

1. Open a profile in a logged-in browser.
2. DevTools → Network → filter `graphql`.
3. Find the request whose response carries the section you want.
4. Copy the hash after the `.` in `queryId`.
5. Update the registry and set `last_verified` to today.

This is an optional compatibility path. The active RSC Experience route does
not use a GraphQL `queryId`; any configured hash is still a dated artifact
from the moment it was captured, and `/v1/health` reports its age.

```bash
uvicorn app.main:app --reload
curl -H "X-API-Key: $KEY" "localhost:8000/v1/profile?url=https://www.linkedin.com/in/samyakj05/"
```

### Deploying

`render.yaml` is a Render blueprint. Set `LINKEDIN_LI_AT`,
`LINKEDIN_JSESSIONID`, `LINKEDIN_COOKIE_HEADER`, and `API_KEYS` in the
dashboard — they are marked `sync: false` so they never enter the repo. A
`Dockerfile` is included for anywhere else.

---

## API reference

### `GET /v1/profile`

| Param | Required | Notes |
|---|---|---|
| `url` | yes | Full URL or bare slug. Accepts locale subdomains, deep links, query strings, percent-encoded unicode. |
| `refresh` | no | Bypass cache. |

Requires `X-API-Key`.

```jsonc
{
  "profile": {
    "public_identifier": "samyakj05",
    "urn": "urn:li:fsd_profile:ACoAA…",
    "full_name": "…",
    "headline": "…",
    "location": { "display": "Bengaluru, Karnataka, India", "country": "India" },
    "connection_count": 500,
    "connection_count_capped": true,   // 500 is a display cap, not a count
    "position_groups": [
      {
        "company_name": "Northwind Systems",
        "positions": [                  // promotion history preserved
          { "title": "Staff Engineer",  "dates": { "start_year": 2024, "is_current": true } },
          { "title": "Senior Engineer", "dates": { "start_year": 2023, "end_year": 2024 } }
        ]
      }
    ],
    "educations": [], "skills": [], "certifications": [], "languages": [],
    "projects": [], "honors": [], "publications": [], "volunteer_experience": []
  },

  "completeness": "partial",
  "sources": [
    { "section": "experience", "tier": "voyager_graphql",
      "query_id": "voyagerIdentityDashProfileComponents.7af5d6f1…",
      "query_id_verified_on": "2026-08-29", "item_count": 4 },
    { "section": "skills", "tier": "public_jsonld" }
  ],
  "warnings": [
    { "code": "degraded_tier", "section": "skills",
      "message": "Section 'skills' was served from the public_jsonld fallback tier…" }
  ],
  "cached": false,
  "elapsed_ms": 1840
}
```

**Schema notes.** Dates are structured, not formatted strings — LinkedIn
routinely gives a year with no month, and a display string throws that
precision away. `endorsement_count: null` means *not visible to this
viewer*, which is not the same as zero; counts are hidden from
non-connections, so defaulting to 0 would assert something false about most
profiles. Every list defaults to empty rather than null.

### `DELETE /v1/profile?url=…`

Purges a cached profile. A cache holding personal data needs a deletion
path, not only an expiry.

### `GET /v1/health`

Unauthenticated, so an uptime check can hit it. Exposes no profile data and
no secrets — only whether a session is configured.

The `queryId` ages are the point. A hash unverified for weeks is the leading
indicator of silent extraction failure.

```jsonc
{
  "status": "ok",
  "session_configured": true,
  "query_registry": [
    { "key": "profile_components", "configured": true, "age_days": 3, "stale": false }
  ],
  "query_registry_warnings": ["profile_cards: no queryId configured"]
}
```

### Errors

Upstream failures map to the status that best describes them **to our
caller**, which is not always what LinkedIn returned. A 999 means nothing to
a client; `502 request_denied` does.

| Status | `error` | Meaning |
|---|---|---|
| 401 | — | Missing or invalid API key |
| 404 | `not_found` | No such profile |
| 422 | — | Not a profile URL (company pages, posts, other hosts) |
| 429 | `rate_limited` | Throttled upstream |
| 502 | `request_denied` | HTTP 999 — UA or IP reputation |
| 502 | `stale_query_id` | A `queryId` was rejected; re-capture it |
| 503 | `challenge_required` | Checkpoint; needs a human in a browser |
| 503 | `session_expired` | Cookie no longer valid |
| 502 | `unexpected_redirect` | Endpoint contract or session context changed |

---

## Testing

```bash
pytest              # 145 tests, no credentials required
```

Fixtures are committed so the suite runs without a LinkedIn session.

**They are hand-authored to the documented response shapes, not recorded
captures.** That is stated plainly because it matters: a suite passing
against invented fixtures proves the parser is internally consistent, not
that it matches what LinkedIn actually returns.
`scripts/capture_fixtures.py` records real responses and sanitises them —
stripping cookies, rewriting member URNs to a stable fake, and replacing
names — so the fixtures can be upgraded to real captures. Raw captures land
in a gitignored directory.

Coverage is weighted toward the things that fail silently:

- **Every rung of the failure ladder**, especially 200 responses that are
  not successes — the interstitial, the auth wall, the checkpoint page
- **Promotion history**, asserted by count and order, since flattening
  produces plausible-looking wrong output
- **Component tree pruning**, the regression guard for the bug above
- **Rest.li encoding**, asserting no JSON braces reach the wire
- **CSRF derivation** and that public-tier headers carry no credentials
- **URL parsing**, including unicode slugs and locale subdomains
- **Health leaks no secrets**

---

## Known limitations

Honestly stated, because most of these are structural rather than fixable.

**1. `queryId` rotation will break tier 1.** Hashes rotate on LinkedIn's
deploy schedule, not ours. The registry and health endpoint expose the age
of a configured hash, while an upstream rejection is surfaced as
`stale_query_id` or `unexpected_redirect`; neither signal proves a newly
captured hash will remain valid. The fix is a human recapturing a successful
section request in DevTools, then updating the registry. That browser use is
for reverse engineering only; deployed requests remain direct HTTP.

**2. LinkedIn's current UI can use RSC routes instead of the Voyager
component query.** The resolver is independently useful, but the section
query is a volatile compatibility path. A deployment must be smoke tested
with its own session and network environment before being advertised as
live. The API fails explicitly on a redirect and never mislabels it as a
successful profile.

**3. The deployed demo may fail from a datacentre IP.** Render, Fly, Railway
and the major clouds occupy well-known ASNs, and LinkedIn's reputation
checks run before your code sees anything. A session cookie harvested at a
home IP and then used from a datacentre is additionally a documented
checkpoint trigger — a location change mid-session. Tier 3 exists precisely
so the demo degrades rather than dies. Production would need residential
proxies with per-account IP affinity, which is out of scope for a hiring
exercise and which I would want to discuss before building.

**4. Sessions last days, not months.** `li_at` is nominally long-lived, but
practitioners report effective Voyager session life of roughly 3–7 days.
Expect to re-harvest. A 401 is the signal.

**5. Rate limiting here is politeness, not evasion.** The pacer keeps
volume low and avoids bursts. It does **not** defeat detection, and I want
to be explicit rather than imply otherwise. LinkedIn's patents
([US11936682B2](https://patents.google.com/patent/US11936682B2/en),
[US11991197B2](https://patents.google.com/patent/US11991197B2/en)) describe
modelling the *sequence* of request paths — tokenised, frequency-ranked, fed
to a deep learning model — and their engineering blog describes an
automation score built specifically to catch low-frequency automation.
Random delays perturb timing while leaving sequence shape unchanged. The
reference library's own datapoint tripped the interstitial at ~900
requests/hour *with* random delays in place.

**6. Component-tree parsing is best-effort by construction.** We extract by
shape rather than exact type name, which survives renames but is less
precise than a schema-aware parser would be if a schema existed. Dates from
tier 1 are parsed from rendered display strings, so they are lossy and
locale-dependent; structured dates from tier 2 are preferred where
available.

**7. Sections not implemented:** recommendations, courses, test scores,
patents, organisations, causes. The mapper pattern extends to them
straightforwardly; they were cut for time, not difficulty.

**8. Not tested against:** profiles with restricted visibility, out-of-network
members, LinkedIn's several profile A/B variants, or non-English locales.
The date parser in particular assumes English month abbreviations.

**9. In-memory cache only.** It does not survive a restart and is not shared
across instances. That is deliberate — persisting scraped personal data
across restarts turns a demo into a database of third parties. A shared
cache would want per-record provenance and a deletion path first.

---

## Legal and data protection

Worth being straight about, since the whole exercise sits here.

**Access.** This uses a secondary account's own session at trivial volume
for a hiring exercise. Authenticated access does breach LinkedIn's User
Agreement §8.2, which prohibits scraping scripts and automated access. The
realistic exposure is account restriction, not litigation — the enforcement
record (hiQ, Mantheos, Proxycurl, ProAPIs) is uniformly against operators
running **fake accounts at commercial scale**, which is a different
activity.

A note on the case everyone cites: *hiQ v. LinkedIn* is widely quoted as
establishing that scraping is legal. It did not. The Ninth Circuit held hiQ
was *likely to succeed* in arguing that scraping **public** data falls
outside the CFAA — a preliminary-injunction standard, never tried. The case
ended in December 2022 with a $500,000 judgment against hiQ, a permanent
injunction, and an order to destroy all scraped data and derived code. The
CFAA holding also has no application to authenticated access, which is what
this is.

**The endpoint is gated.** "Deploy publicly" is not the same as "leave
open". This returns third parties' personal data, so it requires an API key,
caches for minutes rather than indefinitely, exposes a deletion endpoint,
and fails closed if no keys are configured.

**Provenance is a deletion primitive.** The per-section source tracking is
not only an operational nicety. When LinkedIn sued Proxycurl, the prayer for
relief demanded destruction of the scraped data *and* of anything "inferred,
aggregated, or synthesized" from it, *and* the code. If source-derived
fields cannot be identified and quarantined, an order like that is
unsurvivable regardless of the merits. Being able to answer "which fields
came from where" is a schema decision made at build time, not a legal
decision made at complaint time — which is why it is in the response
envelope rather than a logging concern.

**Data protection.** GDPR treats scraped profile data as personal data
regardless of public availability; CNIL's €240,000 KASPR decision (Dec 2024)
is the closest enforcement action to this fact pattern. India's DPDP Act
obligations phase in through May 2027. A production version would need a
documented lawful basis, an Art. 14 notification path, suppression lists so
deleted people do not reappear on the next crawl, and retention limits. None
of that is implemented here, and a demo is not the place to pretend
otherwise.

---

*Built for the Tross engineering challenge, August 2026.*
