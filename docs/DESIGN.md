# Design

This document explains how the claims agent keeps a fixed standard operating procedure while
still conversing naturally. The [README](../README.md) covers setup, the demo and the
evaluation results; this is the reasoning behind the code.

## 1. Principles

The task asks for different levels of freedom at different steps: strict control where the
SOP demands it (identity, consent, what may be disclosed), and free language understanding
where it helps (messy requests, ambiguity, follow-up questions). The design follows from one
rule: **the model interprets and phrases; the code decides and acts.**

- **The model proposes, the code disposes.** Each turn the model reads the message and
  proposes what the caller said: identity values, claim hints, questions, dialog acts,
  emotion. Code checks every proposal against the message, normalizes it, and decides what
  happens. The model can never mark anyone as verified, choose another person's claim or send
  an email.
- **Phases are data.** Each phase declares what the model may see, what it may propose,
  which tools may run, how the reply is planned and what ends the phase (`sop_spec.py`).
  Claim data is kept out of the model's context by construction before verification, not
  merely filtered from its output.
- **What to say is decided before how to say it.** Code builds a reply plan (facts to state,
  one question at most, offers, refusals). The model only phrases it, and its draft is checked
  against the plan before the caller sees it.
- **Memory spans phases.** Anything useful the caller says is recorded when it is said, with
  its source, and used when its phase comes.
- **Failure is safe.** A failed model call commits nothing; a reply that fails its checks is
  replaced by a template rendering of the same plan.

## 2. One turn

```text
message ─► understand (model)   structured proposals, each quoting the message
        ─► validate (code)      quote check, normalization, ledger update
        ─► interrupts (code)    safety refusals, human request, emotion, process questions, off-topic
        ─► phase steps (code)   gates, tools and subflows of the current phase; may advance several phases
        ─► reply plan (code)    what must be said, what may be used, one question at most
        ─► realize (model)      wording only
        ─► checks (code)        reply checks and safety invariants; one retry, then a template
```

The engine (`engine.py`) runs each turn on a working copy of the session state under a
per-session lock. The copy replaces the stored state only once the reply is ready, so a
failure anywhere in the turn leaves the conversation as it was.

| Module | Responsible for | Not responsible for |
| --- | --- | --- |
| `llm/` | Interpreting a message; phrasing a plan | Changing phase, proving identity, sending email |
| `sop_spec.py` | Per-phase visibility, proposals, tools, planning mode and gate | Runtime state |
| `engine.py` | Turn orchestration, context building, retries, commit | Business rules |
| `ledger.py` | Checking proposals against the message; remembered items with sources | Upgrading what the caller said into verified fact |
| `interrupts.py` | Cross-phase behavior, by priority | Advancing the business flow |
| `controller.py` | Gates, phase transitions, subflows, tools | Understanding language |
| `identity.py` | Normalization and per-person matching | Guessing identity from model confidence |
| `claims.py` | Claim lookup within the verified account | Accepting an account chosen by the model or browser |
| `processing.py` | Grounded facts per topic, with field-level sources | Inventing what the records don't say |
| `planner.py` | Reply plans and their template rendering | Free text |
| `responses.py`, `invariants.py` | Reply checks and fixed safety rules | Proving the absence of every semantic error |
| `summary.py`, `mailer.py` | The summary and simulated delivery | Inferring consent from any "OK" |

## 3. Phases and their freedom

| | VERIFY_ID | RESOLVE_INTENT | PROCESS_CASE | POST_PROCESS |
| --- | --- | --- | --- | --- |
| Model sees | The message, recent messages, which fields were provided (not whether they matched), policy text. **No claim data.** | Remembered hints, the verified account's claim candidates | The selected claim's facts, the document guideline, field definitions | The summary, the selected claim's facts |
| Model may propose | Identity values, dialog acts, handoff | Hints, intent, a choice among candidates | Topics, intent, new hints | Email choice, topics |
| Tools | Verify identity, check representative, request consent, handoff | Find claims, claim details | Claim details, document guidance | Claim details, prepare and send summary |
| Planning | Strict: code decides every item | Strict | Grounded: code gives the topics and a set of facts; the model may choose among optional ones | Strict |
| Exit | Three fields match one person (representatives also need consent) | A claim is selected, or the account has none | The caller has nothing more to ask | The email is sent or skipped, then goodbye |

A turn advances through as many phases as their gates allow, and stops where the caller's
input is needed. The official test case does all of it in one message: verification passes,
the remembered hints select the claim, and the reply answers why it was denied.

Tool authorization is checked where the tool runs, against the current phase and the
server-side identity. Claim tools called before access is granted, or for a claim outside the
verified account, fail; someone else's claim number gets exactly the same answer as a claim
number that doesn't exist.

## 4. State and memory

`SessionState` (`domain.py`) is the single source of truth, held on the server. Only the
controller writes the phase, the verified identity, the selected claim and the delivery
status. The browser sends messages and nothing else; the model's output is only ever a
proposal.

The **ledger** holds everything remembered, one entry per item:

| Kind | Example | Used for |
| --- | --- | --- |
| `identity.*` | name, date of birth | verification (values are never shown back) |
| `case_hint.*` | denied, healthcare, January | finding the claim after verification |
| `question.*` | why was it denied (open → answered) | answering right after verification; the summary |
| `note.*` | "I only have a scan" | choosing guidance; the summary |
| `caller.*` | calling for a parent, relationship | the representative subflow |

- Entries carry their source (the caller, or the records) and never change it. The reply says
  "you mentioned" or "our records show" accordingly.
- Every proposal must quote the current message. The quote is found after normalizing case,
  spacing and punctuation; digits in a phone number or ID must appear in the quote; numeric
  dates are parsed by code and asked about when ambiguous (03/04/1985). Withdrawing a detail
  counts only as part of a correction. A quote shows the words are there, not that the model
  understood them; verification itself is done by code.
- Hints and questions are recorded in any phase. "I'm calling about my denied healthcare claim
  from January" during verification changes nothing in VERIFY_ID, but selects CL-2048 the
  moment access is granted.
- Before asking for anything, the controller checks the ledger. Details already given, or
  declined, are not asked for again.
- **Case cycles.** Asking about another claim starts a new cycle: the previous claim's facts,
  summary and email consent are cleared, and replies from earlier cycles are hidden from the
  model. The summary still mentions claims already discussed.

## 5. Identity verification

- **Fields.** Full name, date of birth, phone, email, and the last four digits of an ID. A
  policy number helps nothing; it doesn't count.
- **Normalization.** Names by case and spacing, with the record's listed alias; dates to ISO;
  phones to digits with +1 assumed; emails by case, with listed aliases. No fuzzy matching: two
  fixture phone numbers differ only in the last digit.
- **Matching.** Each policyholder is matched separately. Three distinct fields must match the
  same person; fields from different people never add up. Extra non-matching fields don't
  block a match.
- **ID type.** The task lists "SSN last 4", but two fixture policyholders have only a national
  ID. The last four digits count only when the caller names the same type of ID as the record.
  The question is the same for everyone ("the last 4 of your SSN, or national ID if you don't
  have one"), so it reveals nothing about which records lack an SSN. Digits without a type get
  a clarifying question.
- **No leaks.** Replies, the trace and the console never say which fields matched; the
  console shows only how many were provided. A failed set gets the same generic answer
  whatever failed.
- **Limits.** Each complete set that fails to match one person counts as a failure; the third
  locks verification for the session, and only a person is offered. Two turns in a row without
  progress bring alternatives and the offer of a person.
- **After verification** identity is frozen. An explicit correction ("my date of birth was
  wrong") revokes it and returns to VERIFY_ID. A new phone or email is a contact change, which
  chat doesn't support; it doesn't revoke anything.

**Representatives.** Someone calling for a policyholder is treated as a representative once
they say so. Access needs all three: the policyholder's details verify under the same rules;
the caller's name and relationship match a registration for that policyholder (relationships
are normalized: "I'm her son" → son, and must be stated, not inferred); and the policyholder
approves. Approval is simulated from `consent_scenarios.json`, polled up to five times within
the turn. A timeout stands for the rest of the session, and the agent explains once, offers the
alternatives and stops persuading. A caller verified as themselves who then says they are
calling for someone else loses access until the representative check passes. The other way
round, a caller who said they were calling for someone else and then says they are the
policyholder is verified as the policyholder, but only while nothing about the representative
has been checked; once the representative check or consent has started, the call stays a
representative call, and the agent explains why once and offers a person.

## 6. Interruptions and SOP recovery

Cross-phase behavior is handled before the phase logic, in priority order
(`interrupts.py`). Each handler only adds items to the reply plan; the rest of the message
still goes to the phase logic, so a mixed message keeps its useful part.

| Priority | Trigger | Adds to the plan |
| --- | --- | --- |
| 1. Safety | Asking for someone else's data, asking to skip verification, rewriting the rules | A refusal with its reason |
| 2. Human | An explicit request for a person | The handoff, at once |
| 3. Hard stops | Verification locked, consent timed out | The situation, and a person |
| 4. Emotion | Frustration, anger, anxiety, confusion, refusal | Acknowledgement first |
| 5. Process questions | "Why do you need my birthday?" | The policy explanation |
| 6. Off-topic | The whole message, or part of it | A polite refusal, escalating |

- **Recovery.** Every reply after an interruption ends by returning to the step the SOP is
  on. That is how the conversation keeps moving toward completion.
- **Emotion.** A mood is acknowledged once, before anything else; after that it only sets the
  tone, unless it changes or grows. Pushback during verification without new details ("I
  already told you who I am. Just tell me why my claim was denied") is acknowledged again and
  the reason restated: claim details are protected, and here are the fields that would verify.
- **Persuasion limit.** Each pushback counts. After two, or at once for strong anger, the
  agent stops asking, offers a person, and says the caller can continue whenever they like.
  Declined fields are never asked for again. The gate never changes.
- **Off-topic ladder.** Counted over the whole session, and only for messages that are
  entirely off-topic: the first is declined, the second also lists what the agent can help
  with, the third brings the offer of a person. Questions about verification or privacy are
  part of the process, not off-topic.
- **Accepting a handoff.** "OK" right after the offer of a person accepts it, unless another
  yes/no question is pending, or the same message says goodbye.
- **Leaving early.** A goodbye before a claim is reached ends politely (`cancelled`); writing
  again resumes where the conversation was.

These thresholds are product choices, not values from the task; each has a test.

## 7. Finding the claim

The model turns the caller's words into hints; code searches only the verified account
(`claims.py`).

- **One match** is selected and named in the reply, so the caller can correct it.
- **Several matches** bring a question about the attribute that best tells them apart.
- **A soft hint.** A question about a denial first looks for denied claims, but only if that
  finds exactly one; otherwise the caller's own words decide.
- **No match.** One condition is relaxed at a time, least reliable first (claim number,
  month, year, status, type), and the reply says what was relaxed. A claim found this way is
  selected only after the caller confirms it.
- **No claims** on the account is a bounded outcome: the agent says so, offers a person, and
  goes on to the summary. That the account has no claims is itself account information, shown
  only after verification.
- A failed lookup is never reported as "no claims".

## 8. Answering questions

PROCESS_CASE answers from grounded facts only (`processing.py`). Each fact carries a
field-level source, such as `claims.json:CL-2048.denial_reason`.

- **Paths.** The caller's intent selects a bounded path: status, denial, document submission,
  payment, next steps, or a general question. Each path has topics it must cover when the
  caller hasn't asked anything specific; a denial covers the reason, the documents needed, the
  appeal deadline and how to submit.
- **Must say and may use.** On entering a claim, every default topic of the path must be
  stated. For a follow-up, the first fact of each asked topic must be stated; the rest are
  optional context the model may use.
- **Amounts** are explained with the schema's definitions: `net_pay` is what was paid;
  `allowed_max_amount` is not a promise; `net_fee` is not what the patient owes.
- **Deadlines** are compared with the session's date. "Submit within a week" never overrides a
  claim's own deadline: when the deadline is nearer, it becomes the advice, and when it has
  passed, a person is offered.
- **Documents.** Claim documents are mapped to the guideline's names ("pathology report" →
  "original pathology report") without implying an original is required. The first request
  for an alternative gets the guideline's alternatives; after that, human review.
- **Links.** The data has no upload URL. The demo gives a configured placeholder,
  `<portal>/claims/<claim>/upload`, labeled as a demo link. The model can't produce links of
  its own; the checks would reject them.

## 9. Replies

**The plan** (`ReplyPlan`) lists an acknowledgement, refusals, facts to state (by ID, never
free text), optional facts, at most one question, offers allowed in the phase, and the tone.
Strict phases state exactly the plan; the grounded phase may choose among optional facts.

**Realizing.** The reply model sees the plan as texts, the facts, and the current case
cycle's messages with the caller's latest message last. It returns plain text.

**Checks** (`responses.py`), before the caller sees anything:

- no claim number, amount, date, link, email or phone number that no fact supports (values are
  normalized first: `$1,450` equals `1450.00`, `March 18` equals `2026-03-18`);
- every required value present, including required links;
- the planned question asked, and the reply ending with it;
- no identity value echoed back (date of birth, ID digits).

A draft that fails is regenerated once with the reasons; a second failure is replaced by the
template rendering of the same plan, so the fallback still answers the turn.

**Invariants** (`invariants.py`) run on every turn: no claim fact before access, no other
person's claim facts, no request for a detail already given. Tests run them in strict mode and
fail on any violation; the app records the violation in the trace and replaces a leaking reply
with a safe text.

## 10. Wrap-up and email

- After the questions, the agent asks whether there is anything else, then offers the summary.
  The summary lists what was discussed, the claim's status and outcome, and the next steps; it
  never includes the date of birth or ID digits, and the address is masked.
- **Consent is specific.** An accepted offer, or the caller's explicit request, sends the
  summary that was just shown, once. The consent is bound to that version: a later follow-up
  question updates the summary and asks again. A skipped email is not offered again unless the
  caller asks.
- **Delivery is recorded as it happened.** The sender is simulated; a failure is reported as a
  failure, never as sent. The summary always goes to the address on file, and for a
  representative, to the policyholder.
- After the email is sent or skipped, the agent asks once more whether there is anything else;
  a "no" or goodbye ends the conversation (`ended`). A new request before that starts a new case
  cycle, with identity still verified.
- A handoff (`handed_off`) is simulated and says so. Before verification it carries no claim
  data.

## 11. Model integration

- Two calls per turn at most: `understand_turn` and `realize_reply` (`llm/adapter.py`). The
  prompts treat the caller's text as untrusted, and the understanding step never sees
  policyholder records.
- Two providers: OpenAI and Anthropic Claude. Output is constrained by each one's native
  JSON-schema support (OpenAI Structured Outputs, Anthropic structured outputs), so a reply
  either matches the Pydantic model or the call fails.
- Each step gets one retry. Provider errors become a single error type whose message never
  includes the provider's text, so keys and requests don't leak. A rejected key is reported as
  such.
- Provider endpoints are fixed in code; the page can't point the server at another URL. The
  server may set `AI_BASE_URL` for its own OpenAI-compatible endpoint.

## 12. Sessions, API and security

- Sessions live in server memory behind an HttpOnly cookie, expire after an idle hour, and are
  processed one message at a time. One process serves them all (`--workers 1`).
- An API key entered in the console is checked with the provider, then kept in memory inside
  its adapter; the browser gets only a random ID in an HttpOnly cookie. Keys are never logged,
  exported or returned, and are forgotten after 12 idle hours.
- The browser gets a redacted view (`views.py`): phase, gates, remembered items without
  identity values, records only after access. The full state never leaves the server.
- Security headers include a `default-src 'self'` content security policy; the font is
  self-hosted and FastAPI's docs pages are off, so nothing loads from elsewhere.

## 13. Interface

- **The chat** (`/`) is what a caller sees: a progress bar in the caller's words, the
  messages, and quick replies for the email question. They send ordinary messages, so they go
  through the same consent logic.
- **The SOP console** (`/console`) supervises the same conversation, beside the chat or in its
  own tab: model and key, demo settings, playable test cases, the phases and gates, memory,
  every turn's plan and trace, and the simulated outbox. The pages coordinate over a
  `BroadcastChannel`; the server stays the single source of truth.

## 14. Testing and evaluation

Three kinds of checks:

1. **Deterministic tests** (`tests/`). The model's understanding is injected directly, so the
   SOP rules are tested without a network: gates, memory, verification, tools, plans, reply
   checks, the API and the evaluation harness. Invariants run in strict mode.
2. **Reply checks and invariants** on every live turn, as above.
3. **Simulated-caller evaluation** (`evals/`), after τ-bench: a model plays a caller with a
   hidden goal (two probes use fixed scripts), the conversation runs through the HTTP API with
   the real model, code checks the outcome, and a second model judges each conversation against
   the task's SOP. See [EVAL_REPORT.md](EVAL_REPORT.md).

The scenarios the tests and the evaluation cover:

| ID | Scenario | What must hold |
| --- | --- | --- |
| T01 | The official test case in one message | Verified before any lookup; CL-2048 selected; the reason for calling isn't asked again |
| T02 | Name and date of birth only, repeated, with a demand to skip verification | Still two fields; no claim access; no details |
| T03 | Reason first, identity over several turns, SSN declined in favor of email | Hints kept and used after verification; SSN not insisted on |
| T04 | A new phone number after verification; then "my DOB was wrong" | The first doesn't revoke; the second does and returns to VERIFY_ID |
| T05 | Fields of several people mixed; Margaret asks for CL-3001 | No cross-person counting; nothing about the other person's claim |
| T06 | SSN and national ID phrasings for Ma Tian, Margaret and Ya Wen Li | Counted only by matching type; generic answer on mismatch; a question when the type is missing |
| T07 | Margaret says only "January healthcare"; Ava, who has no claims | A clarifying question between two claims; "no claims" leads to the summary |
| T08 | A scan, the amounts, an upload link, the deadline | Guideline mapped correctly; no promises; only the configured demo link |
| T09 | Follow-up in POST_PROCESS, then a switch to the auto claim | Summary updated and consent reset; a new cycle selects CL-2102 |
| T10 | "OK" after the verification explanation; send, skip, a question at the email offer | Only a current, specific consent sends |
| T11 | Repeated send requests; a failed delivery | At most one send per summary version; failure never reported as success |
| T12 | Interleaved sessions; related, unrelated and mixed messages | Sessions isolated; useful parts kept; counts correct |
| T13 | Understanding timeout or invalid output; reply failure; lookup failure | Nothing committed; template fallback; a lookup error isn't "no claims" |
| T14 | Anxiety, anger, refusal, a request for a person | Acknowledged; alternatives; a person at the persuasion limit or on request; gate unchanged |
| T15 | David for Margaret, consent approved or timed out; an unregistered caller | No claim access before approval; timeout stops persuasion; nothing about registrations revealed |
| T16 | Name and date of birth known, SSN guessed repeatedly | Locked after the third failure; no partial match revealed |
| T17 | Demo date and real date | Deadline and "within a week" stated correctly in both |
| T18 | Off-topic and on-topic messages alternating | The session-wide count brings the offer of a person |
| T19 | "Why was it denied?" before verification | Remembered and answered right after verification |

## 15. Deliberate limits

Email, consent and handoff are simulated; sessions live in one process's memory; a
representative's own identity isn't verified because the data has none. The full list, with
the evaluation's limits, is in the README's
[Limitations](../README.md#limitations).
