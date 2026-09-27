# SOP-guided conversational agent for insurance claims

A claims-support chat agent that follows a fixed procedure,
`VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`, and still talks naturally. Code
controls the phases, the safety gates and every action; the language model only reads each
message and words the reply the code has decided on.

## What's submitted

| | |
| --- | --- |
| **Live demo** | <https://sop-claims-agent-p733.onrender.com>, deployed from this repository's `main` branch |
| **GitHub repository** | <https://github.com/BeichenHuang/sop_guided_conversation> |
| **Code archive (zip)** | The same code as the repository |

## Try the live demo

1. Open the link. It runs on a free instance, so the first visit after a quiet spell takes
   about a minute.
2. In the **SOP console** beside the chat, under **Model**, choose OpenAI or Anthropic Claude,
   paste an API key and choose **Use this key**. The key stays in the server's memory, for
   your browser only.
3. Under **Test cases**, pick **Official test case** and choose **Play the conversation**.
   The chat fills in step by step, and each step says what to look for. Seven more cases cover
   a frustrated caller, a son calling for his mother, a missing SSN, someone else's claim and
   off-topic questions. You can also type in the chat yourself, as one of the people below.

While it runs, the console shows **Workflow** (the current phase and its gates), **Memory**
(what the caller said, kept for later phases), **Turns** (what the code decided to say on each
turn, and the rules it applied) and **Outbox** (the simulated email).

| Person | Name | Date of birth | Phone | Email | ID last 4 | Claims |
| --- | --- | --- | --- | --- | --- | --- |
| P9 | Margaret Chen | 1985-03-15 | 650-521-2836 | margaret@email.com | SSN 4472 | CL-2048 (healthcare, denied), CL-2011, CL-1899, CL-2102 |
| P7 | Ava Lopez | 1990-08-21 | 650-388-2920 | ava.lopez@email.com | SSN 9180 | none |
| P12 | Ma Tian | 1964-09-10 | 650-208-8799 | matian@example.com | national ID 6688 | CL-3001 (healthcare, denied) |
| P13 | Ya Wen Li | 1989-12-03 | 650-521-2830 | yawen.li@gmail.com | national ID 5317 | none |

David Chen is registered as Margaret's son and may call for her.

## Run it yourself

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp .env.example .env        # put an OpenAI key in AI_API_KEY; never commit .env
uv run --env-file .env python -m uvicorn apps.insurance_claims.api:app --port 8000
```

Or with Docker:

```bash
docker build -t sop-agent .
docker run --rm -p 8000:8000 --env-file .env sop-agent
```

Then open <http://localhost:8000>. Instead of `AI_API_KEY`, a key can be entered in the
console, as in the live demo. Without a key the app starts, and the chat waits until one is
entered.

| Provider | Default model | Also suggested in the console | Evaluated |
| --- | --- | --- | --- |
| OpenAI | `gpt-6-luna` | `gpt-6-sol` | yes, 39 of 39 |
| Anthropic Claude | `claude-opus-5` | `claude-sonnet-5`, `claude-haiku-4-5` | yes, 39 of 39 |

The console's Model field also takes any other model name the key can use. Settings,
the HTTP API and deployment are in [docs/REFERENCE.md](docs/REFERENCE.md).

## How it works

```text
message ─► understand (model)   structured proposals, each quoting the message
        ─► validate (code)      quote check, normalization, memory update
        ─► interrupts (code)    safety refusals, human request, emotion, off-topic
        ─► phase steps (code)   gates and tools of the current phase only
        ─► reply plan (code)    what must be said, at most one question
        ─► realize (model)      wording only
        ─► checks (code)        reply checks and safety rules; one retry, then a template
```

- **Phases are data.** Each phase declares what the model may see, what it may propose,
  which tools may run and what ends it. No claim data reaches the model before the caller is
  verified.
- **The model proposes, code decides.** The model reports what the caller said (identity
  details, claim hints, questions, emotion); code checks each item against the message and
  decides. Three details must match one policyholder before anything about a claim is shared.
- **Memory across phases.** A hint like "my denied healthcare claim from January", given
  during verification, is kept and used to find the claim once verification passes.
- **Checked replies.** A reply may state only facts from the records, and is checked for
  unsupported numbers, dates, IDs and links before the caller sees it.
- **Emotion without shortcuts.** Frustration is acknowledged and the reason for each step is
  explained; after repeated refusals the agent offers a person. No gate is ever lowered.

The full design is in [docs/DESIGN.md](docs/DESIGN.md).

## Evaluation

- **351 deterministic tests** run without a network: they feed the model's understanding in
  directly and check the SOP rules. `uv run pytest`
- **A simulated-caller evaluation** runs 13 scenarios, 3 times each, through the real app: a
  second model plays callers with hidden goals, code checks each outcome, and a judge model
  reviews each conversation against the SOP.

| Agent | Passed | Safety violations | Template fallbacks | Judge flags (in 273 rulings) |
| --- | --- | --- | --- | --- |
| `gpt-6-luna` | 39/39 | 0 | 0 | 2 |
| `claude-opus-5` | 39/39 | 0 | 0 | 2 |

The judge's flags are answer-quality points, such as answering "what if my appeal is
approved?" with the recorded amounts instead of saying plainly that the records don't show
it. Every run, finding and fix is in [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md).

## How the task's requirements are met

| The task asks for | How this agent does it |
| --- | --- |
| Strict verification with at least 3 identity details | Three distinct details must match one policyholder, checked by code; no claim data is shown or sent to the model before that. |
| Natural conversation during verification | Details can come in any order; declined details aren't asked for again and other options are offered; "why do you need that?" gets the reason. |
| Freer reasoning for intent and case handling | The model interprets the request; code searches only the verified account, asks what tells claims apart, and answers from the records. |
| An email summary the caller can accept or skip | Shown first, sent only with consent (to a simulated outbox), never twice. |
| Polite refusal of off-topic questions, then a human | Declined each time; the second time lists what the agent can do, the third offers a person. |
| Remembering details for later phases | Hints, questions and notes are kept with their source from any phase. |
| Bonus: empathy, persuasion and escalation | Feelings are acknowledged, reasons explained, alternatives offered; after two refusals, or at once for strong anger, a person is offered. |

## Limitations

- Email, the policyholder's consent and the handoff to a person are simulated, and upload
  links point to a placeholder portal; the app says so.
- Conversations live in server memory and end after an idle hour or a restart.
- Calls for someone else are basic: the caller's own identity isn't verified (the data has
  none), and consent is asked once per conversation.
- The evaluation is small (three runs per scenario) and its judge is a model.
- Conversations and the interface are in English only.

**Notes.** The test data is consistent only in March 2026, so the agent treats 2026-03-10 as
today; **Demo settings** in the console switches to the real date, where appeal deadlines
have passed. The task lists "SSN last 4", but two people in the data have only a national ID,
so the last four digits count only when the caller names the same type of ID as the record.
