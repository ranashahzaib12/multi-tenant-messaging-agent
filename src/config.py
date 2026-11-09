import os
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
FROM_WHATSAPP_NUMBER = os.getenv("FROM_WHATSAPP_NUMBER", "")

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Google OAuth client (Web application type) — used to build the client
# config in-memory rather than requiring a credentials.json file on the
# deployed host (that file is never committed since it holds the secret).
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

# Embeddings for document search. Must stay OpenAI text-embedding-3-small
# (1536 dims): it's what the legacy assistant's pa_knowledge_chunks knowledge store uses, and the
# document store has to be comparable with it. Without a key, documents are
# still stored and searched by keyword.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# The Claude model every turn (manager + subagents) runs on. Pinned to Sonnet
# 5 for cost (~2.5x cheaper than the SDK's Opus default); override on Railway
# to compare quality without a deploy.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

# Shared secret for POST /webhook/test. That endpoint runs the agent as any
# phone number it's given and returns the reply, so leaving it open would let
# anyone read any user's notes and documents. Unset = endpoint disabled.
TEST_WEBHOOK_TOKEN = os.getenv("TEST_WEBHOOK_TOKEN", "")

# Phone numbers (comma-separated, E.164) allowed to query the business tables
# (leads, products, documents). Those tables aren't keyed by user, so without
# this gate anyone who texts the number could read the client's customer
# list. Unset = nobody.
BUSINESS_DATA_PHONES = {p.strip() for p in os.getenv("BUSINESS_DATA_PHONES", "").split(",") if p.strip()}

# Who may use the assistant at all (E.164, comma-separated). Empty = open to
# anyone who messages the number (the state before the closed test). When set,
# owner numbers are always included; everyone else gets one short notice and
# no agent turn, so a stranger can't run up the bill or get a session.
ALLOWED_PHONES = {p.strip() for p in os.getenv("ALLOWED_PHONES", "").split(",") if p.strip()}

WHATSAPP_CHAR_LIMIT = 1600

PORT = int(os.getenv("PORT", "5000"))

# The app's own public HTTPS base URL once deployed (e.g.
# "https://your-app.example.com"), no trailing slash.
# Used to build a fixed Google OAuth redirect_uri rather than guessing it
# from request headers, which is unreliable behind a reverse proxy.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
