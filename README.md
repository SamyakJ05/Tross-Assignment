# LinkedIn Profile API

A direct-HTTP LinkedIn profile API for the Tross engineering challenge. It accepts a public LinkedIn
profile URL and returns structured JSON. Runtime code does not launch or control a browser.

The implementation uses two currently observed LinkedIn request families:

- Dash REST for identity data such as name, headline, location, and profile images.
- RSC profile-card streams for experience, education, and certifications.

Browser DevTools was used only while developing the request contracts. The deployed API and CLI make
ordinary HTTP requests with the two session values supplied through environment variables or the POST body.

**Live deployment:** https://linkedin-profile-api-z73g.onrender.com — interactive docs at
[`/docs`](https://linkedin-profile-api-z73g.onrender.com/docs), unauthenticated health check at
[`/v1/health`](https://linkedin-profile-api-z73g.onrender.com/v1/health). `/v1/profile` requires an
`X-API-Key`; request one from the maintainer rather than expecting the example key in `.env.example`
to work.

## Assignment checklist

| Requirement | Status |
|---|---|
| Public HTTPS API | Deployed on Render: https://linkedin-profile-api-z73g.onrender.com |
| LinkedIn profile URL input | `GET /v1/profile?url=...` |
| Structured profile JSON | Schema includes every requested profile field |
| Own backend credentials | Server-managed or caller-provided LinkedIn session support |
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
# Required for server-managed GET requests.
LINKEDIN_LI_AT=
LINKEDIN_JSESSIONID=

# Required by the HTTP API. Use a long random value.
API_KEYS=replace-with-a-long-random-api-key
```

The API constructs the request Cookie header from these two values. Use the POST endpoint below when each
caller should supply their own session instead of storing one on the server.

### Run the HTTP API

```bash
uv run uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive OpenAPI interface.

```bash
curl -s \
  -H 'X-API-Key: replace-with-a-long-random-api-key' \
  'http://127.0.0.1:8000/v1/profile?url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fexample%2F'
```

Use `refresh=true` only when deliberately making a new upstream request. Normal requests use the
15-minute in-memory cache. A lower-quality refresh result never overwrites a stronger cached result.

One `Pacer` (see `app/linkedin/client.py`) is shared by every request the process handles, so the
minimum-interval throttle to LinkedIn is process-wide rather than reset per request. Independently,
each `X-API-Key` gets its own request budget (`RATE_LIMIT_PER_MINUTE`, default 20/minute) so one
caller cannot consume the whole shared LinkedIn-facing budget alone.

### Run the CLI

The CLI uses the same direct-HTTP service but does not require an API key because it is local.

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

### `GET /v1/profile`

| Input | Required | Description |
|---|---:|---|
| `url` | Yes | Full LinkedIn profile URL or public identifier |
| `refresh` | No | Bypass the cache and attempt a new upstream fetch |
| `X-API-Key` | Yes | One value from `API_KEYS` |

### `POST /v1/profile` with your own session

Use this endpoint when testing with your own LinkedIn session instead of the server's `.env` session.
The credential values are accepted only in the JSON request body, are marked write-only in OpenAPI, are not
cached, and are never returned by the API. Do not send them in the URL or through `GET` query parameters.

```bash
curl -sS -X POST 'http://127.0.0.1:8000/v1/profile' \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: replace-with-a-long-random-api-key' \
  --data '{
    "url": "https://www.linkedin.com/in/example/",
    "credentials": {
      "LINKEDIN_LI_AT": "your-li-at-value",
      "LINKEDIN_JSESSIONID": "your-jsessionid-value"
    }
  }'
```

Both values are required. The API constructs the necessary Cookie header internally; do not send a complete
browser Cookie header.

#### Find the two values in Chrome

Use only a LinkedIn account you own or are authorized to use. Never paste the values into Git, screenshots,
issue trackers, or chat.

1. Sign in to LinkedIn in Chrome and open a profile page normally.
2. Open DevTools with `Option` + `Command` + `I`.
3. Open **Application** → **Storage** → **Cookies** →
   `https://www.linkedin.com`. Copy the `Value` cells for `li_at` and `JSESSIONID` into
   `LINKEDIN_LI_AT` and `LINKEDIN_JSESSIONID`.
4. Send the POST request only to `https://` in a deployed environment. `http://127.0.0.1` is appropriate
   only for local testing.

If LinkedIn returns `challenge_required`, `session_expired`, `unexpected_redirect`, or `request_denied`, sign
in normally, complete any challenge, capture a fresh session, and retry later. Do not automate the login flow.

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

### `DELETE /v1/profile`

Purges the in-memory cache entry for one profile URL. This is included because cached profile data needs a
direct deletion path.

### `GET /v1/health`

Unauthenticated. Reports version, whether a session is configured, cache state, and optional GraphQL query
registry status. It does not expose session values or API keys.

## Approach

1. Validate the profile URL and derive its public identifier.
2. Attempt Dash REST first for the top card. This preserves the tested identity request order before
   detailed cards are requested.
3. Request three RSC profile cards directly over HTTP: experience, above activity, and below activity.
   Separately, fetch the `/details/skills` sub-page's own RSC pagination action -- a different action
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

The suite includes API authentication and cache behavior, URL validation, session header handling, RSC
stream decoding, current RSC card layouts, promotion grouping, request-scoped credential handling, mapper
behavior, CLI output, and error classification.

Before submitting:

1. Run the three commands above.
2. Run one local API request with a fresh burner session and confirm the response fields and `sources`.
3. Confirm `git status --short` is empty and `git ls-files .env` prints nothing.
4. Push the repository publicly to GitHub. **Done** — see the live deployment link above.
5. Deploy the `render.yaml` blueprint and set the listed secrets in Render. **Done.**
6. Confirm `<render-url>/v1/health` and one authenticated `GET /v1/profile` request over HTTPS. **Done**
   — both verified against the live deployment above; rotate the `API_KEYS` secret in Render before
   final submission since a placeholder value was used during development.

## Render deployment

Deployed at https://linkedin-profile-api-z73g.onrender.com. `render.yaml` creates the web service with
only the API-access secret. The caller-provided POST flow needs no LinkedIn credentials on Render. Add
the two optional LinkedIn secrets manually only if you later want the server-managed GET flow:

| Secret | Required |
|---|---:|
| `API_KEYS` | Required |
| `LINKEDIN_LI_AT` | Optional: enables server-managed GET requests |
| `LINKEDIN_JSESSIONID` | Optional: enables server-managed GET requests |

Render supplies `PORT`; the start command binds to it automatically. The public endpoint remains protected
by `X-API-Key`.

## Limitations

- LinkedIn endpoints are undocumented and can change without notice. The API returns typed warnings for
  redirects, checkpoints, throttling, and changed payloads.
- A session can be challenged or rejected after an upstream sequence changes. Refresh the two session values
  in a normal browser before retrying. Do not add a login flow to this API.
- The request-scoped credential endpoint is for controlled testing. A public deployment must use HTTPS and
  an API key; callers should provide only their own authorized session and treat it as a password.
- Field visibility is account-, relationship-, locale-, and experiment-dependent. Missing `about`, skills,
  languages, or images means the current cards did not expose an unambiguous value; the service does not
  invent one.
- Dates parsed from RSC display strings are best-effort and currently assume English month names.
- The in-memory cache is intentionally short-lived and per-process. It is not a persistent profile database.
- A cloud IP can receive LinkedIn HTTP 999 or a checkpoint even when the same session works locally. The
  API surfaces that as a typed response instead of parsing an error page as profile data.
- The GraphQL tier (`app/linkedin/queries.py`) ships with every `query_id` blank, and evidence from live
  capture is that LinkedIn's current profile UI no longer calls the old `profileComponents` query at
  all -- it has moved to per-section RSC pagination actions instead, the same way experience and
  education already work in this codebase. Skills now has its own RSC path (`app/linkedin/rsc.py`'s
  `fetch_skills`/`skill_names`, keyed off the member's `profileId` rather than the vanity slug).
  Languages, projects, honors, publications, and volunteer experience still have no working source and
  will be empty for every profile; each would need its own `/details/<section>` pagination action
  captured from DevTools the same way skills was, since the GraphQL route they were meant to use is
  believed dead. Education and certifications are still partially covered by the RSC below-activity
  card's regex heuristics in the meantime.
- The skills RSC path fetches only the first 50 skills in one page (no multi-page crawl) and cannot
  recover endorsement counts -- those require a different response shape than what's captured today. A
  profile with more than 50 skills will show only the first 50, and endorsement counts will always be
  `null` regardless of visibility.

## Security

`.env`, virtual environments, build artifacts, and OS metadata are ignored by Git. The API fails closed if
`API_KEYS` is unset. Secrets are never returned from `/v1/health`, logged by the application, or included
in the repository.

Built for the Tross Software Engineer hiring challenge.
