# Multi-Tenant WhatsApp Agent

A multi-tenant personal assistant on WhatsApp, built with the **Claude Agent SDK**.
One manager agent delegates to 12 specialist subagents across **54 tools** covering
Google Workspace, a Postgres store, headless browser automation and web research.

Tenancy is keyed per phone number. Each user gets their own Claude Agent SDK session,
their own Google OAuth grant, and their own row-scoped data.

---

## Architecture

```
User's WhatsApp
      │
   Twilio                       WhatsApp bridge, inbound webhook is signature-verified
      │
   FastAPI (app.py)             webhook + OAuth callback routes + reminder scheduler
      │
   Claude Agent SDK             claude_agent_sdk.query(), model claude-sonnet-5
      │
   ┌──────────────────────────────────────────────────────────────┐
   │  Manager agent                                               │
   │    permission_mode="dontAsk"                                 │
   │    every filesystem and shell built-in explicitly disallowed │
   │                                                              │
   │  12 specialist subagents (AgentDefinition)                   │
   │    email · calendar · tasks · drive · workspace · contacts   │
   │    notes · documents · memory · reminders · data · images    │
   │    research · browser                                        │
   └──────────────────────────────────────────────────────────────┘
      │
      ├── Google APIs        Gmail, Calendar, Drive, Tasks, Contacts, Docs, Sheets
      ├── PostgreSQL         per-user tables + read-only business query surface
      ├── Playwright         headless Chromium, guest bookings on public sites
      └── WebSearch/WebFetch Claude Code built-ins (no third-party search key)
```

### Request flow

1. Twilio POSTs an inbound WhatsApp message to `/whatsapp/webhook`, where the request
   signature is verified and the sender checked against an allowlist.
2. `_prepare_message` resolves any pending booking approval from the user's own text.
3. `_attach_media` turns voice into text and attachments into indexed chunks.
4. `PersonalAssistant.ainvoke` builds `ClaudeAgentOptions` for that phone number and
   calls `query()`.
5. The manager delegates to subagents; tools run in-process through
   `create_sdk_mcp_server`.
6. The reply is split to the 1600-character WhatsApp limit and sent back.

A background loop polls due reminders once a minute and delivers them through the
same agent path.

### Why the Claude Agent SDK rather than a raw API client

The SDK is Claude Code packaged as a library, which gives native multi-agent
orchestration without a hand-rolled supervisor loop. `ClaudeAgentOptions.agents`
defines named subagents, each with its own prompt and tool allow-list, and the model
delegates to them through the built-in Task mechanism.

**Authentication** uses a real `ANTHROPIC_API_KEY` from
[platform.claude.com](https://platform.claude.com). Anthropic's terms do not permit a
deployed third-party product to ride on a personal claude.ai or Claude Code
subscription login, so a metered API key is the only correct option. The SDK reads it
from the environment automatically.

---

## Safety controls

These are enforced in code, not in prompt text. They are the interesting part of this
repository for anyone evaluating agent tool-use safety.

| Control | Location | What it enforces |
|---|---|---|
| Filesystem and shell removal | `src/agents/assistant.py` | Read, Write, Edit, Bash, Glob, Grep, LS and every other built-in touching the filesystem or a shell is in `disallowed_tools`. Added after the assistant once read its own source and quoted environment variable names back to a user. |
| Booking approval gate | `src/utils/approvals.py`, `src/tools/browser/booking.py` | The final commit-style click in a browser booking is refused unless the user said yes in their own inbound message. The approval is parsed in code, so the model cannot grant it to itself by calling a tool. Expires after 10 minutes. |
| Login and payment refusal | `src/tools/browser/booking.py` | Elements whose label looks like a login are refused outright. |
| Read-only business queries | `src/database.py` | Single statement only, must start with SELECT or WITH, executed in a read-only session under a dedicated reader role, 10 second statement timeout, wrapped in an outer LIMIT, then rolled back. |
| Business data allowlist | `src/config.py` | The shared business tables are reachable only from an explicit allowlist of phone numbers. |
| Agent access allowlist | `src/config.py` | Only allowlisted numbers get an agent turn at all. |
| Test endpoint secret | `app.py` | `POST /webhook/test` returns 404 unless a shared-secret header matches. Unset secret disables the route. |
| Session isolation | `src/agents/assistant.py` | The ambient session id is stripped from the subprocess environment so it cannot override the explicit per-user session. |
| Per-user serialisation | `src/agents/assistant.py` | One asyncio lock per phone number, so turns for a user never interleave. |

### Per-user isolation

- **Session**: each phone number gets an explicit Claude Agent SDK `session_id`,
  generated on first contact and passed back via `resume` on later turns. The SDK's
  default session selection is never relied on, because it can silently attach to an
  ambient session.
- **Tools**: per-user tools are rebuilt for every inbound message by factories that
  close over the calling phone number, rather than trusting the model to pass an
  internal user id back correctly.
- **Google**: each user completes their own OAuth grant through a signed, short-lived
  connect link. There is no shared mailbox or shared calendar.

### Design principles carried over

- **Truth about actions.** The manager prompt treats a claim of "done" as valid only
  if a subagent actually returned a tool result that turn. Confidently claiming to
  have created an event that was never attempted was the single most common failure
  in the predecessor system.
- **Principles, not lists.** Prompts avoid enumerating accepted words or formats.
  Models miss edge cases that lists do not cover, and sometimes recite the list back
  to the user as advice.
- **Fail closed on missing identity.** Tools require an explicit user id. There is no
  "return everything" fallback.
- **Ambient state is not identity.** Session ids are always assigned explicitly.

---

## Setup

### 1. Environment

```bash
cp .env.example .env
```

`src/config.py` is the authoritative list of what is actually read. `OPENAI_API_KEY`
is optional: without it, voice transcription is unavailable and document search falls
back to keyword matching.

### 2. Anthropic API key

Set `ANTHROPIC_API_KEY` from [platform.claude.com](https://platform.claude.com). The
SDK picks it up from the environment. `CLAUDE_MODEL` overrides the default
`claude-sonnet-5`.

### 3. Google OAuth

Each user connects their own Google account. The server is headless, so it uses a web
callback flow and builds the client config from environment variables rather than a
committed `credentials.json`.

1. Create a project in [Google Cloud Console](https://console.cloud.google.com) and
   enable the Gmail, Calendar, Drive, Tasks, People, Docs and Sheets APIs.
2. Create an OAuth 2.0 client of type **Web application**. Desktop-type clients only
   accept loopback redirects and will not work here.
3. Add `https://<your-domain>/oauth/google/callback` to the Authorized redirect URIs.
4. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
5. Set `PUBLIC_BASE_URL` to the app's own public HTTPS URL, no trailing slash. It is
   used to build the exact redirect URI, since inferring it from proxy headers is
   unreliable.
6. A user connects by asking the assistant on WhatsApp for a connect link, which is
   signed and short-lived.

### 4. Twilio

1. Get a WhatsApp-enabled Twilio number, sandbox for testing or an approved sender for
   production.
2. Point its webhook at `https://<your-domain>/whatsapp/webhook`.
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `FROM_WHATSAPP_NUMBER` as
   `whatsapp:+<number>`.

### 5. Database

```bash
DATABASE_URL=postgresql://user:pass@host:port/dbname
```

`init_database()` creates the per-user tables and indexes with `IF NOT EXISTS` on
first boot, and the read-only reader role creates itself. Without `DATABASE_URL` the
app still starts; data-backed tools return errors while Google and browser tools keep
working.

### 6. Deploy

The included `Procfile` runs `uvicorn app:app --host 0.0.0.0 --port $PORT` and is
picked up by any Procfile-aware host. A `Dockerfile` is included for container hosts.

`token.json` is written to local disk by default. On hosts with an ephemeral
filesystem, mount a volume and point `GOOGLE_TOKEN_FILE` inside it, or tokens are
lost on redeploy.

For local development, `python app.py` auto-reloads; expose it with
`ngrok http 5000`.

---

## Testing without WhatsApp

`POST /webhook/test` runs the agent as any phone number and returns the reply
directly. It requires the `X-Test-Token` header matching `TEST_WEBHOOK_TOKEN`, and
returns 404 when that variable is unset.

```bash
curl -X POST http://localhost:5000/webhook/test \
  -H "X-Test-Token: $TEST_WEBHOOK_TOKEN" \
  -d "phone=+10000000000" \
  -d "message=What is on my calendar this week?"
```

Note that the body is form-encoded, not JSON, and the reply comes back under the
`reply` key.

---

## Project structure

```
app.py                      FastAPI entry: WhatsApp webhook, OAuth routes,
                            media route, reminder scheduler, health check
Procfile / Dockerfile       deployment
src/
  config.py                 environment configuration, allowlists
  database.py               Postgres schema, per-user queries, read-only SQL guard
  agents/assistant.py       builds ClaudeAgentOptions, 12 AgentDefinitions, runs query()
  channels/whatsapp.py      Twilio sender, typing indicator, message splitting
  prompts/                  manager prompt plus one prompt per subagent
  tools/
    calendar/               get, create, update, delete events, connect link
    email/                  search, read, read attachment, save to Drive, modify, send
    drive/                  search, read, save last file
    workspace/              create and append Docs, create, append, read Sheets
    tasks/                  list, add, complete Google Tasks
    contacts/               search Google Contacts
    notes/                  add, list, search notes
    documents/              list, search, delete stored documents
    reminders/              create, list, cancel, stop reminders
    memory/                 remember, forget, recall user facts
    data/                   legacy record readers plus read-only business queries
    images/                 image generation, chart rendering
    browser/                open, read, click, type, select, screenshot,
                            request_booking_approval
  utils/
    approvals.py            booking approval gate
    google_auth.py          per-user OAuth, token refresh, scope checks
    browser.py              Playwright session pool and element indexing
    documents.py            text extraction, chunking, embeddings
    media_ai.py             transcription, image generation
    media_store.py          short-lived media links
    message_splitter.py     1600-character WhatsApp limit
    recent_files.py         recently received file tracking
```

---

## Known characteristics

Stated plainly, since this repository is published for independent review.

- The agent runs with `permission_mode="dontAsk"`. Tool calls execute without an
  interactive confirmation step. Safety rests on the code-level gates listed above and
  on model judgement.
- Tool results are returned to the model as text. A tool that refuses an action
  reports that refusal in its return value.
- The prompt corpus and tool descriptions are English only.
- `POST /webhook/test` can act as any phone number by design, which is why it is
  disabled unless a shared secret is configured.
