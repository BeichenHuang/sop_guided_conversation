# Reference

Details the [README](../README.md) leaves out: the two pages, configuration, the HTTP API,
tests, the project layout and deployment.

## The two pages

Both pages show the conversation in this browser.

- **The chat** (`/`) is what a caller sees: a progress bar in the caller's words (*Confirm
  it's you*, *Find your claim*, *Get answers*, *Wrap up*), the messages, *Yes / No* buttons for
  the email summary, and **New conversation**. The **SOP console** switch shows the console
  beside the chat; *Open console in a new tab* shows it on its own.
- **The SOP console** (`/console`) supervises the workflow and holds the demo controls:
  - **Model**: which model replies and whose key it uses; enter a key here. Marked in amber
    while no real model replies.
  - **Demo settings**, folded to one line: the date the agent treats as today (demo or real),
    and whether the simulated policyholder approves a representative's consent request. They
    apply from the next conversation.
  - **Test cases**: eight complete scripts, each step with what to look for; **Play the
    conversation** sends them all, **Send next step** one at a time.
  - **Workflow**: the four phases, the gates (identity shows how many details were provided,
    never which matched; representative and consent when relevant; the claim; the email
    summary), and what the agent's last question is waiting for.
  - **Turns**: one card per turn, newest first, with the caller's message, the reply, the reply
    plan (what the code decided to say) and the trace (the rules applied and the tools called).
    Template fallbacks and invariant violations are flagged. *Download* saves the conversation
    as Markdown or JSON.
  - **Memory**: what the caller said, apart from what came from the records. Identity fields
    appear only as "provided"; records only after access is granted.
  - **Outbox**: the simulated emails, in full.

## Configuration

All settings are environment variables. API keys are read only on the server; they are never
sent to the browser or written to logs.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_API_KEY` | – | The model key: OpenAI unless `AI_PROVIDER` says otherwise. Optional, since a key can be entered in the page. |
| `AI_PROVIDER` | `openai` if a key is set, else `fake` | `openai` or `anthropic`, or `fake` for the stand-in. |
| `AI_MODEL` | the provider's default | Model for both steps. With OpenAI, `gpt-6-sol` is a stronger, pricier option. |
| `AI_REASONING_EFFORT` | `low` | `none`, `low`, `medium` or `high`. Leave empty for models without reasoning. |
| `AI_BASE_URL` | – | Optional OpenAI-compatible endpoint for `AI_PROVIDER=openai`. |
| `EMAIL_MODE` | `mock` | Email summaries are always simulated. |
| `DEMO_AS_OF_DATE` | `2026-03-10` | The "today" of demo-date mode (see the README's notes). |
| `CONSENT_SCENARIO` | `default` | Simulated policyholder consent for representatives: `default` (approved) or `timeout`. |
| `DEMO_PORTAL_URL` | `https://portal.example.com` | Placeholder member portal; each claim's upload link is `<portal>/claims/<case id>/upload`, labeled as a demo link. |
| `COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS. |
| `CONSENT_POLL_INTERVAL` | `1` | Seconds between simulated consent polls (0–10). |
| `STRICT_INVARIANTS` | `false` | Raise instead of replacing the reply when a safety invariant is violated (used by tests). |

## HTTP API

| Method and path | Purpose |
| --- | --- |
| `POST /api/sessions` | Start a conversation (optional `date_mode`, `consent_scenario`). Sets an HttpOnly session cookie. |
| `GET /api/session` | Current session view and message history. |
| `POST /api/session/messages` | Send `{"message": "..."}`; returns the reply, session view, reply plan and trace. |
| `GET /api/session/outbox` | Simulated emails for this session (only after verification). |
| `GET /api/session/console` | What the SOP console shows: the session view, every turn's messages, reply plan and trace, and the outbox. Doesn't count as activity. |
| `GET /api/session/export` | The same as a file: `?format=markdown` (default) or `?format=json`. |
| `DELETE /api/session` | End the conversation and clear the cookie. |
| `GET /api/model` | Which model replies in this browser: `source` is `yours`, `server` or `none`. Never the key; `key_hint` has its last four characters. |
| `PUT /api/model/key` | Check `{"provider": "...", "api_key": "...", "model": "..."}` with the provider and use it for this browser. |
| `DELETE /api/model/key` | Forget this browser's key. |
| `GET /healthz` | Process status and which model adapter is active; never returns secrets. |

Errors use the shape `{"error": {"code": "...", "message": "..."}}`: `401 session_expired`,
`403 invalid_key` (the provider rejected the key during a turn), `422 invalid_request`,
`422 invalid_key` and `422 key_check_failed` (a key that can't be used), `503 model_unavailable`,
`500 internal_error`.

## Tests and CI

```bash
uv run pytest
uv run ruff check apps tests evals && uv run ruff format --check apps tests evals
```

The deterministic tests inject the model's understanding directly, so they
check the SOP rules without a network. They cover scenarios T01–T19 in
[DESIGN.md](DESIGN.md#14-testing-and-evaluation), plus the API, the pages' markup and
the evaluation harness. CI (`.github/workflows/ci.yml`) runs the linter and the tests, builds
the Docker image, and smoke-tests a conversation turn in the running container.

## Project layout

```text
apps/insurance_claims/
  api.py              HTTP routes, session cookie, static UI
  sop_spec.py         what each phase may see, propose and call, and its exit gate
  engine.py           per-turn orchestration: understand → plan → phrase → check
  controller.py       phase steps: verification, consent, claim lookup, answers, email summary
  interrupts.py       safety refusals, requests for a human, emotions, off-topic handling
  ledger.py           checks model proposals against the message, then remembers them
  identity.py         identity normalization and per-person matching
  consent.py          representative check and simulated policyholder consent
  claims.py           claim lookup limited to the verified account
  processing.py       grounded facts for questions about the selected claim
  planner.py          reply plans and their template rendering
  responses.py        checks a model reply must pass before it is shown
  invariants.py       safety rules checked on every turn
  summary.py          the summary shown at the end and sent by email
  mailer.py           email sending interface and the simulated sender
  domain.py           enums, model-facing contracts, session and API models
  views.py            the redacted session view, with the console's gates, memory and turns
  store.py, keys.py   in-memory sessions, and API keys entered in the page
  transcript.py       the console's view as a Markdown or JSON file
  fixture_loader.py   loads and cross-checks the fixture files
  config.py           environment settings and the injectable clock
  llm/                provider-neutral interface, provider adapters, stand-in, prompts
  fixtures/           read-only test data
  static/             chat page, SOP console, test case scripts
evals/                simulated-caller evaluation: scenarios, simulator, judge, runner, results
tests/                pytest suite
docs/                 design, evaluation report, this reference
render.yaml           Render Blueprint for the live demo
```

## Deploying to Render

`render.yaml` describes the live demo on [Render](https://render.com)'s free plan, built from the
Dockerfile.

1. In the Render dashboard, choose **New > Blueprint** and connect the repository. Leave
   *Blueprint Path* empty.
2. Render asks for `AI_API_KEY`. Leave it empty, and every visitor enters their own key in the
   SOP console; or set it, and visitors without a key use yours (set a budget on the key first).
3. Deploy. Each push to `main` redeploys. The free plan sleeps after 15 idle minutes; waking
   takes about a minute and clears sessions and keys entered in the page.
