# LinkedIn Profile API

A direct-HTTP LinkedIn profile API for the Tross engineering challenge. It accepts a public LinkedIn
profile URL and returns structured JSON. Runtime code does not launch or control a browser.

The implementation uses two currently observed LinkedIn request families:

- Dash REST for identity data such as name, headline, location, and profile images.
- RSC profile-card streams for experience, education, and certifications.

Browser DevTools was used only while developing the request contracts. The deployed API and CLI make
ordinary HTTP requests with the server's two session values supplied through environment variables.

**Live deployment:** https://linkedin-profile-api-z73g.onrender.com. Interactive docs are at
[`/docs`](https://linkedin-profile-api-z73g.onrender.com/docs), and health check at
[`/v1/health`](https://linkedin-profile-api-z73g.onrender.com/v1/health). A caller only supplies the
LinkedIn profile URL; LinkedIn session credentials stay in the backend.

## Assignment checklist

| Requirement | Status |
|---|---|
| Public HTTPS API | Deployed on Render: https://linkedin-profile-api-z73g.onrender.com |
| LinkedIn profile URL input | `GET /profile?url=...` |
| Structured profile JSON | Schema includes every requested profile field |
| Own backend credentials | Server-managed LinkedIn session |
| Public GitHub source | Pushed to GitHub; no secrets tracked |
| Setup, API, approach, and limitations | This README |

Fields are returned when LinkedIn makes them visible through the current endpoint contracts. The response
reports provenance and warnings instead of silently treating unavailable fields as empty profile data.

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <your-public-repository-url>
cd Tross-Assignment-2
uv sync --extra dev
cp .env.example .env
```

Configure `.env` locally. Never commit it.

```dotenv
# Required for full profile results.
LINKEDIN_LI_AT=
LINKEDIN_JSESSIONID=
```

The API constructs LinkedIn request headers from these backend-only values. Callers never send them.

### Run the HTTP API

```bash
uv run uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive OpenAPI interface.

```bash
curl -s --get \
  --data-urlencode 'url=https://www.linkedin.com/in/example/' \
  'http://127.0.0.1:8000/profile'
```

Use `refresh=true` only when deliberately making a new upstream request. Normal requests use the
15-minute in-memory cache. A lower-quality refresh never replaces a stronger cached result, and a good
expired result can be served for up to 24 hours if LinkedIn fails transiently.

One `Pacer` (see `app/linkedin/client.py`) is shared by every request the process handles, so the
minimum-interval throttle to LinkedIn is process-wide rather than reset per request. Identical concurrent
lookups are coalesced. Each client IP gets its own request budget (`RATE_LIMIT_PER_MINUTE`, default
20/minute).

### Run the CLI

The CLI uses the same direct-HTTP service.

```bash
uv run linkedin-profile https://www.linkedin.com/in/example/
uv run linkedin-profile example --compact
```

Exit status `0` means a profile response was received. Upstream session, challenge, throttling, or URL
errors are emitted as JSON to standard error with a non-zero exit status.

### Run with Docker

```bash
docker build -t linkedin-profile-api .
docker run --rm -p 8000:8000 --env-file .env linkedin-profile-api
```

Then use the same `curl` command above.

## API

### `GET /profile`

| Input | Required | Description |
|---|---:|---|
| `url` | Yes | Full LinkedIn profile URL or public identifier |
| `refresh` | No | Bypass the cache and attempt a new upstream fetch |

`GET /v1/profile` remains as a hidden backward-compatible alias. The public OpenAPI page exposes only
the canonical URL-only operation above.

Example response, shortened:

```json
{
  "profile": {
    "public_identifier": "example",
    "full_name": "Asha Raman",
    "headline": "Software Engineer",
    "location": { "display": "Bengaluru, India" },
    "position_groups": [
      {
        "company_name": "Northwind",
        "positions": [
          {
            "title": "Software Engineer",
            "dates": { "start_year": 2024, "is_current": true }
          }
        ]
      }
    ],
    "educations": [],
    "skills": [],
    "certifications": [],
    "languages": []
  },
  "completeness": "partial",
  "sources": [
    { "section": "top_card", "tier": "voyager_dash_rest" },
    { "section": "experience", "tier": "linkedin_rsc" }
  ],
  "warnings": [],
  "cached": false
}
```

`completeness` is one of:

- `complete`: load-bearing identity and history fields arrived without a fallback tier.
- `partial`: useful data arrived through a thinner tier.
- `needs_review`: the upstream response was incomplete or suspicious, for example RSC data without a
  name because Dash redirected.

### `GET /v1/health`

Reports version, whether a session is configured, cache state, and optional GraphQL query registry status.
It does not expose session values.

## Approach

1. Validate the profile URL and derive its public identifier.
2. Attempt Dash REST first for the top card. This preserves the tested identity request order before
   detailed cards are requested.
3. Request three RSC profile cards directly over HTTP: experience, above activity, and below activity.
   Separately, fetch the `/details/skills` sub-page's own RSC pagination action, which uses a different
   shape (`actions/pagination`, not `actions/component`) that the GraphQL components query used to cover
   before LinkedIn stopped calling it (see step 5).
4. Parse visible card strings into grouped positions, education, and certifications without fabricating
   unavailable values. Parse skill names from the pagination stream's endorse-button labels.
5. Optionally use a configured GraphQL query as a compatibility path for additional sections.
6. Return source provenance, warnings, and a completeness state with the normalized profile.

The request pacer limits calls and the cache avoids repeatedly fetching the same profile. Neither is an
attempt to evade LinkedIn controls.

## Testing and release checks

All automated tests use committed, sanitized fixtures and require no LinkedIn session.

```bash
uv run ruff check .
uv run pytest -q
uv build
```

The suite includes the public URL-only API contract, cache behavior, URL validation, session header handling,
RSC stream decoding, current RSC card layouts, promotion grouping, mapper behavior, CLI output, and error
classification.

Before submitting:

1. Run the three commands above.
2. Run one local API request with a fresh burner session and confirm the response fields and `sources`.
3. Confirm `git status --short` is empty and `git ls-files .env` prints nothing.
4. Push the repository publicly to GitHub. **Done**. See the live deployment link above.
5. Deploy the `render.yaml` blueprint and set the listed secrets in Render. **Done.**
6. Confirm `<render-url>/v1/health` and one `GET /profile?url=...` request over HTTPS.

## Render deployment

Deployed at https://linkedin-profile-api-z73g.onrender.com. `render.yaml` creates the web service. Add
the two LinkedIn secrets in the Render dashboard; they are never stored in the blueprint or repository:

| Secret | Required |
|---|---:|
| `LINKEDIN_LI_AT` | Required for full profile results |
| `LINKEDIN_JSESSIONID` | Required for full profile results |

Render supplies `PORT`; the start command binds to it automatically. Callers provide only a LinkedIn URL.

## Limitations

- LinkedIn endpoints are undocumented and can change without notice. The API returns typed warnings for
  redirects, checkpoints, throttling, and changed payloads.
- A session can be challenged or rejected after an upstream sequence changes. Refresh the two session values
  in a normal browser before retrying. Do not add a login flow to this API.
- Field visibility is account-, relationship-, locale-, and experiment-dependent. Missing `about`, skills,
  languages, or images means the current cards did not expose an unambiguous value; the service does not
  invent one.
- Dates parsed from RSC display strings are best-effort and currently assume English month names.
- The in-memory cache is intentionally short-lived and per-process. It is not a persistent profile database.
- A cloud IP can receive LinkedIn HTTP 999 or a checkpoint even when the same session works locally. The
  API surfaces that as a typed response instead of parsing an error page as profile data.
- The GraphQL tier (`app/linkedin/queries.py`) ships with every `query_id` blank, and evidence from live
  capture is that LinkedIn's current profile UI no longer calls the old `profileComponents` query at
  all. It has moved to per-section RSC pagination actions instead, the same way experience and
  education already work in this codebase. Skills now has its own RSC path (`app/linkedin/rsc.py`'s
  `fetch_skills`/`skill_names`, keyed off the member's `profileId` rather than the vanity slug).
  Languages, projects, honors, publications, and volunteer experience still have no working source and
  will be empty for every profile; each would need its own `/details/<section>` pagination action
  captured from DevTools the same way skills was, since the GraphQL route they were meant to use is
  believed dead. Education and certifications are still partially covered by the RSC below-activity
  card's regex heuristics in the meantime.
- The skills RSC path fetches up to 100 skills in one bounded page and cannot recover endorsement counts;
  those require a different response shape. Endorsement counts remain `null` regardless of visibility.

## Security

`.env`, virtual environments, build artifacts, and OS metadata are ignored by Git. LinkedIn session values
are backend-only and are never returned from `/v1/health`, logged by the application, or included in the
repository. The public endpoint is rate-limited by client IP.

Built for the Tross Software Engineer hiring challenge.
