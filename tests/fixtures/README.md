# Fixtures

These payloads are **hand-authored to match the response shapes documented in
`app/parsing/normalized.py` and `app/parsing/components.py`**. They are not
recorded captures.

That distinction matters and is stated here rather than buried, because a
test suite that passes against invented fixtures proves the parser is
internally consistent — not that it matches what LinkedIn actually returns.

**To replace them with real captures:**

```bash
python scripts/capture_fixtures.py --slug <a-profile-you-control>
```

The script writes raw responses to `tests/fixtures/raw/` (gitignored),
strips identifying values, and emits sanitised versions here. Capture from a
profile you own or have permission to use, never an arbitrary member's.

**What gets stripped:** member URNs are rewritten to a stable fake, names and
locations replaced, image URLs truncated to their path shape, and every
`li_at`/`JSESSIONID`/`csrf-token` value removed. Read the diff before
committing — the sanitiser is best-effort, not a guarantee.

| File | Shape |
|---|---|
| `top_card_dash.json` | Dash REST `identity/dash/profiles` response, normalized format |
| `experience_components.json` | GraphQL section response with one multi-role employer and one single-role |
| `education_components.json` | GraphQL section response |
| `skills_components.json` | GraphQL section response |
| `public_profile.html` | Public page carrying a schema.org JSON-LD block |
| `interstitial.html` | The HTML rate-limit page LinkedIn serves with a 200 |
