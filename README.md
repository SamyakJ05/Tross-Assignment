# LinkedIn Profile API

A direct-HTTP LinkedIn profile API for the Tross engineering challenge. It accepts a public LinkedIn
profile URL and returns structured JSON. Runtime code does not launch or control a browser.

The implementation uses two currently observed LinkedIn request families:

- Dash REST for identity data such as name, headline, location, and profile images.
- RSC profile-card streams for experience, education, and certifications.

Browser DevTools was used only while developing the request contracts. The deployed API and CLI make
ordinary HTTP requests with a session supplied through environment variables.

## Assignment checklist

| Requirement | Status |
|---|---|
| Public HTTPS API | Render blueprint included; deploy after GitHub publication |
| LinkedIn profile URL input | `GET /v1/profile?url=...` |
| Structured profile JSON | Schema includes every requested profile field |
| Own backend credentials | Environment-only LinkedIn session support |
| Public GitHub source | Local Git repository ready to push; no secrets tracked |
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
# Required by current RSC profile-card endpoints. Copy the complete Cookie
# request-header value from a currently authenticated LinkedIn request.
LINKEDIN_COOKIE_HEADER=

# Required by the HTTP API. Use a long random value.
API_KEYS=replace-with-a-long-random-api-key
```

`LINKEDIN_LI_AT` and `LINKEDIN_JSESSIONID` are also supported for Dash REST, but RSC card requests
normally need the fuller `LINKEDIN_COOKIE_HEADER` context. The header must contain both `li_at` and
`JSESSIONID`.

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
4. Parse visible card strings into grouped positions, education, and certifications without fabricating
   unavailable values.
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
stream decoding, current RSC card layouts, promotion grouping, mapper behavior, CLI output, and error
classification.

Before submitting:

1. Run the three commands above.
2. Run one local API request with a fresh burner session and confirm the response fields and `sources`.
3. Confirm `git status --short` is empty and `git ls-files .env` prints nothing.
4. Push the repository publicly to GitHub.
5. Deploy the `render.yaml` blueprint and set the listed secrets in Render.
6. Confirm `<render-url>/v1/health` and one authenticated `GET /v1/profile` request over HTTPS.

## Render deployment

`render.yaml` creates the web service. In the Render dashboard, set these secret environment variables:

| Secret | Required |
|---|---:|
| `LINKEDIN_COOKIE_HEADER` | Yes for current RSC cards |
| `LINKEDIN_LI_AT` | Optional Dash fallback |
| `LINKEDIN_JSESSIONID` | Optional Dash fallback |
| `API_KEYS` | Yes |

Render supplies `PORT`; the start command binds to it automatically. The public endpoint remains protected
by `X-API-Key`.

## Limitations

- LinkedIn endpoints are undocumented and can change without notice. The API returns typed warnings for
  redirects, checkpoints, throttling, and changed payloads.
- A session can be challenged or rejected after an upstream sequence changes. Refresh the burner session in
  a normal browser, then copy its full Cookie request-header value into the deployment secret. Do not add a
  login flow to this API.
- Field visibility is account-, relationship-, locale-, and experiment-dependent. Missing `about`, skills,
  languages, or images means the current cards did not expose an unambiguous value; the service does not
  invent one.
- Dates parsed from RSC display strings are best-effort and currently assume English month names.
- The in-memory cache is intentionally short-lived and per-process. It is not a persistent profile database.
- A cloud IP can receive LinkedIn HTTP 999 or a checkpoint even when the same session works locally. The
  API surfaces that as a typed response instead of parsing an error page as profile data.

## Security

`.env`, virtual environments, build artifacts, and OS metadata are ignored by Git. The API fails closed if
`API_KEYS` is unset. Secrets are never returned from `/v1/health`, logged by the application, or included
in the repository.

Built for the Tross Software Engineer hiring challenge.
