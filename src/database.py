import json
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from src.config import DATABASE_URL

logger = logging.getLogger(__name__)


@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Aria runs against the legacy assistant's Supabase database, where n8n Aria is still live and
# owns the pa_* tables (pa_users, pa_reminders, pa_personas, pa_knowledge_chunks).
# While both systems run in parallel, Aria writes only to its own agent_*
# tables: writing to pa_users would double-count every message on the live
# user's row (both bots handle each one). the legacy assistant's tables are read-only to Aria
# until n8n Aria is switched off and Aria takes them over.
def _lock_tables(cur, *tables: str) -> None:
    """Enable row-level security with no policies.

    Supabase serves every table in the public schema over its REST API, and
    the anon key that API accepts is designed to be public. A table without
    RLS is therefore readable by anyone holding that key — for these tables,
    users' notes and uploaded documents (which can be health records). RLS
    with no policies denies the REST API entirely, matching the legacy assistant's pa_* tables;
    this app connects as the table owner, which bypasses RLS, so it's unaffected.
    """
    for table in tables:
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def init_database():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_users (
                id SERIAL PRIMARY KEY,
                phone TEXT UNIQUE NOT NULL,
                name TEXT,
                timezone TEXT DEFAULT 'Asia/Singapore',
                profile JSONB DEFAULT '{}',
                message_count INT DEFAULT 0,
                last_msg_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS agent_notes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT[] DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS agent_chat_history (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS agent_reminders (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'reminder',
                text TEXT NOT NULL,
                due_at TIMESTAMPTZ NOT NULL,
                recurrence TEXT NOT NULL DEFAULT 'none',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INT NOT NULL DEFAULT 0,
                last_error TEXT,
                last_sent_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_agent_users_phone ON agent_users(phone);
            CREATE INDEX IF NOT EXISTS idx_agent_reminders_due ON agent_reminders(status, due_at);
            CREATE INDEX IF NOT EXISTS idx_agent_notes_user ON agent_notes(user_id);
            CREATE INDEX IF NOT EXISTS idx_agent_chat_history_user ON agent_chat_history(user_id);
        """)
        _lock_tables(cur, "agent_users", "agent_notes", "agent_chat_history", "agent_reminders")
        conn.commit()
        logger.info("Database tables initialized")

    # Separate transaction: if pgvector isn't available, only document search
    # degrades — notes and chat history above are already committed.
    try:
        init_document_tables()
    except Exception as e:
        logger.warning("Document tables not initialized: %s", e)
    try:
        init_business_reader()
    except Exception as e:
        logger.warning("Business reader role not set up (business data tools will refuse): %s", e)


def init_document_tables():
    """Per-user uploaded documents (PDF/DOCX/text) and their searchable chunks.

    Only extracted text is kept, not the original file: less sensitive data
    held at rest, and deletion is a single cascade. embedding is nullable so a
    document is still stored and keyword-searchable without an OpenAI key.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE EXTENSION IF NOT EXISTS vector;

            CREATE TABLE IF NOT EXISTS agent_documents (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INT,
                char_count INT,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                supersedes_id INT REFERENCES agent_documents(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS agent_document_chunks (
                id SERIAL PRIMARY KEY,
                document_id INT NOT NULL REFERENCES agent_documents(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                chunk_index INT NOT NULL,
                content TEXT NOT NULL,
                embedding vector(1536)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_documents_user ON agent_documents(user_id);
            CREATE INDEX IF NOT EXISTS idx_agent_document_chunks_user ON agent_document_chunks(user_id);
        """)
        _lock_tables(cur, "agent_documents", "agent_document_chunks")
        logger.info("Document tables initialized")


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def find_document_by_hash(user_id: str, content_hash: str) -> dict | None:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, filename, created_at FROM agent_documents WHERE user_id = %s AND content_hash = %s",
            (user_id, content_hash),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def save_document(
    user_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    content_hash: str,
    chunks: list[str],
    embeddings: list[list[float]] | None,
) -> dict:
    """Store a document and its chunks in one transaction.

    A re-upload under the same filename supersedes the previous active copy
    (kept, but excluded from search) rather than deleting it: the user may
    still want the old version, and deletion should be their explicit choice.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id FROM agent_documents
               WHERE user_id = %s AND filename = %s AND status = 'active'
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, filename),
        )
        prev = cur.fetchone()
        supersedes_id = prev[0] if prev else None
        if supersedes_id:
            cur.execute(
                "UPDATE agent_documents SET status = 'superseded' WHERE id = %s AND user_id = %s",
                (supersedes_id, user_id),
            )

        cur.execute(
            """INSERT INTO agent_documents
               (user_id, filename, content_type, size_bytes, char_count, content_hash, supersedes_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (user_id, filename, content_type, size_bytes, sum(len(c) for c in chunks), content_hash, supersedes_id),
        )
        doc_id = cur.fetchone()[0]

        for i, chunk in enumerate(chunks):
            emb = _vector_literal(embeddings[i]) if embeddings else None
            cur.execute(
                """INSERT INTO agent_document_chunks (document_id, user_id, chunk_index, content, embedding)
                   VALUES (%s, %s, %s, %s, %s::vector)""",
                (doc_id, user_id, i, chunk, emb),
            )
        return {"id": doc_id, "supersedes_id": supersedes_id}


def list_documents(user_id: str) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, filename, content_type, char_count, status, supersedes_id, created_at
               FROM agent_documents WHERE user_id = %s ORDER BY created_at DESC""",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def search_document_chunks(
    user_id: str, query: str, query_embedding: list[float] | None, limit: int = 5
) -> list[dict]:
    """Semantic search when an embedding is available, keyword search otherwise.
    Superseded documents are excluded; always scoped to user_id."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if query_embedding:
            cur.execute(
                """SELECT c.content, c.chunk_index, d.id AS document_id, d.filename,
                          1 - (c.embedding <=> %s::vector) AS score
                   FROM agent_document_chunks c
                   JOIN agent_documents d ON d.id = c.document_id
                   WHERE c.user_id = %s AND d.user_id = %s AND d.status = 'active'
                     AND c.embedding IS NOT NULL
                   ORDER BY c.embedding <=> %s::vector LIMIT %s""",
                (_vector_literal(query_embedding), user_id, user_id, _vector_literal(query_embedding), limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                return rows
        cur.execute(
            """SELECT c.content, c.chunk_index, d.id AS document_id, d.filename, NULL AS score
               FROM agent_document_chunks c
               JOIN agent_documents d ON d.id = c.document_id
               WHERE c.user_id = %s AND d.user_id = %s AND d.status = 'active'
                 AND c.content ILIKE %s
               ORDER BY d.created_at DESC, c.chunk_index LIMIT %s""",
            (user_id, user_id, f"%{query}%", limit),
        )
        return [dict(r) for r in cur.fetchall()]


def delete_document(user_id: str, document_id: int) -> str | None:
    """Hard delete (chunks cascade). Returns the filename, or None if the
    document doesn't exist for this user — never touches another user's rows."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM agent_documents WHERE id = %s AND user_id = %s RETURNING filename",
            (document_id, user_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


def upsert_user(phone: str, name: str | None = None) -> dict:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO agent_users (phone, name, message_count, last_msg_at)
            VALUES (%s, %s, 1, NOW())
            ON CONFLICT (phone) DO UPDATE SET
                message_count = agent_users.message_count + 1,
                last_msg_at = NOW(),
                updated_at = NOW()
            RETURNING *
        """, (phone, name))
        return dict(cur.fetchone())


def get_profile(phone: str) -> dict:
    """What Aria knows about a user: its own agent_users row merged with
    the read-only bits of the legacy assistant's pa_users (name, timezone, core_prompt), so a
    user who set Aria up under n8n is recognised here without being asked
    again. Aria's own values win when both exist."""
    out: dict = {
        "name": None, "timezone": None, "facts": {}, "core_prompt": None, "persona": None,
        "legacy_user_id": None, "session_id": None,
    }
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "SELECT id, name, timezone, persona_name, profile->>'core_prompt' AS core_prompt FROM pa_users WHERE phone = %s",
                (phone,),
            )
            legacy_row = cur.fetchone()
        except Exception:
            conn.rollback()
            legacy_row = None
        if legacy_row:
            # Aria keys everything else (knowledge chunks, logs, personas) by
            # this uuid, not by phone.
            out.update(
                legacy_user_id=str(legacy_row["id"]), name=legacy_row["name"], timezone=legacy_row["timezone"],
                core_prompt=legacy_row["core_prompt"], persona=legacy_row["persona_name"],
            )
        cur.execute("SELECT name, timezone, profile FROM agent_users WHERE phone = %s", (phone,))
        mine = cur.fetchone()
        if mine:
            stored = dict(mine["profile"] or {})
            out["session_id"] = stored.get("_session_id")
            facts = {k: v for k, v in stored.items() if not k.startswith("_")}
            out["facts"] = facts
            out["name"] = facts.get("name") or mine["name"] or out["name"]
            # The column has a default, so only a timezone the user actually
            # told us (in facts) may override the one Aria already had.
            out["timezone"] = facts.get("timezone") or out["timezone"] or mine["timezone"]
    return out


def remember_facts(phone: str, facts: dict) -> dict:
    """Merge facts into the user's profile. 'name' and 'timezone' also land in
    their own columns so other code can read them without parsing JSON."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """INSERT INTO agent_users (phone, name, timezone, profile)
               VALUES (%s, %s, COALESCE(%s, 'Asia/Singapore'), %s::jsonb)
               ON CONFLICT (phone) DO UPDATE SET
                   profile = COALESCE(agent_users.profile, '{}'::jsonb) || EXCLUDED.profile,
                   name = COALESCE(EXCLUDED.profile->>'name', agent_users.name),
                   timezone = COALESCE(EXCLUDED.profile->>'timezone', agent_users.timezone),
                   updated_at = NOW()
               RETURNING profile""",
            (phone, facts.get("name"), facts.get("timezone"), json.dumps(facts)),
        )
        return dict(cur.fetchone()["profile"] or {})


def get_session_id(phone: str) -> str | None:
    """The user's Claude session id, kept in their profile under a
    leading-underscore key so it never shows up as a 'fact'."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT profile->>'_session_id' FROM agent_users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def set_session_id(phone: str, session_id: str) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO agent_users (phone, profile) VALUES (%s, %s::jsonb)
               ON CONFLICT (phone) DO UPDATE SET
                   profile = COALESCE(agent_users.profile, '{}'::jsonb) || EXCLUDED.profile,
                   updated_at = NOW()""",
            (phone, json.dumps({"_session_id": session_id})),
        )


def get_google_tokens(phone: str) -> str | None:
    """This user's Google OAuth tokens (the credentials JSON), stored under an
    underscore key so they are never surfaced as a 'fact'."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT profile->>'_google_tokens' FROM agent_users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def set_google_tokens(phone: str, tokens_json: str) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO agent_users (phone, profile) VALUES (%s, %s::jsonb)
               ON CONFLICT (phone) DO UPDATE SET
                   profile = COALESCE(agent_users.profile, '{}'::jsonb) || EXCLUDED.profile,
                   updated_at = NOW()""",
            (phone, json.dumps({"_google_tokens": tokens_json})),
        )


def forget_fact(phone: str, key: str) -> bool:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE agent_users SET profile = COALESCE(profile, '{}'::jsonb) - %s, updated_at = NOW()
               WHERE phone = %s AND profile ? %s""",
            (key, phone, key),
        )
        return cur.rowcount > 0


# --- the legacy assistant's knowledge base (read-only) ---------------------------------------
# Documents the user gave the n8n Aria live in pa_knowledge_chunks, keyed by
# pa_users.id. Aria reads them so nothing the user stored before is lost;
# it never writes there (see the parallel-run rule at the top of this file).

def list_canon_sources(legacy_user_id: str) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT source, COUNT(*)::int AS chunks, MIN(created_at) AS created_at,
                      SUM(LENGTH(content))::int AS char_count
               FROM pa_knowledge_chunks WHERE user_id = %s::uuid
               GROUP BY source ORDER BY MIN(created_at)""",
            (legacy_user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def search_canon(legacy_user_id: str, query: str, query_embedding: list[float] | None, limit: int) -> list[dict]:
    """Semantic search through the legacy assistant's match_cue_knowledge (which itself fails
    closed without a user_id filter), keyword fallback without an embedding."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if query_embedding:
            cur.execute(
                """SELECT k.source, k.section, k.chunk_index, m.content, m.similarity
                   FROM match_cue_knowledge(%s::vector, %s, %s::jsonb) m
                   JOIN pa_knowledge_chunks k ON k.id = m.id AND k.user_id = %s::uuid""",
                (_vector_literal(query_embedding), limit, json.dumps({"user_id": legacy_user_id}), legacy_user_id),
            )
        else:
            cur.execute(
                """SELECT source, section, chunk_index, content, NULL::float AS similarity
                   FROM pa_knowledge_chunks
                   WHERE user_id = %s::uuid AND content ILIKE %s
                   ORDER BY source, chunk_index LIMIT %s""",
                (legacy_user_id, f"%{query}%", limit),
            )
        return [dict(r) for r in cur.fetchall()]


# --- the legacy assistant's per-user records (read-only, keyed by pa_users.id) ----------------

def list_cue_logs(legacy_user_id: str, kind: str | None, days: int, limit: int) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT kind, logged_at, value_num, data, note, source FROM pa_logs
               WHERE user_id = %s::uuid AND (%s::text IS NULL OR kind = %s)
                 AND logged_at >= NOW() - (%s || ' days')::interval
               ORDER BY logged_at DESC LIMIT %s""",
            (legacy_user_id, kind, kind, str(int(days)), limit),
        )
        return [dict(r) for r in cur.fetchall()]


def list_cue_personas(legacy_user_id: str) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT name, slug, description, LENGTH(content)::int AS chars FROM pa_personas
               WHERE user_id = %s::uuid ORDER BY name""",
            (legacy_user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_cue_persona(legacy_user_id: str, slug: str) -> dict | None:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT name, slug, description, content FROM pa_personas WHERE user_id = %s::uuid AND slug = %s",
            (legacy_user_id, slug),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_cue_reminders(legacy_user_id: str, status: str | None, limit: int) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT title, notes, due_at, status, fired_at, recurrence FROM pa_reminders
               WHERE user_id = %s::uuid AND (%s::text IS NULL OR status = %s)
               ORDER BY due_at DESC LIMIT %s""",
            (legacy_user_id, status, status, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def search_cue_chat_history(phone: str, query: str, limit: int) -> list[dict]:
    """the legacy assistant's n8n memory is keyed 'pa-<phone>'; each row is one LangChain message."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT message->>'type' AS role, message->>'content' AS content, created_at
               FROM n8n_chat_histories
               WHERE session_id = %s AND message->>'content' ILIKE %s
               ORDER BY created_at DESC LIMIT %s""",
            (f"pa-{phone}", f"%{query}%", limit),
        )
        return [dict(r) for r in cur.fetchall()]


# --- Business tables (owner-only, read-only) -----------------------------------
# leads / products / documents belong to the client's WhatsApp sales bot and
# aren't keyed by user. Access is gated per phone in the tool layer, and
# read-only-ness is enforced by Postgres itself: queries run as a role that
# can only SELECT from these three tables, inside a READ ONLY transaction,
# so no amount of clever SQL from the model reaches pa_users or writes.
BUSINESS_TABLES = ("leads", "products", "documents")
READER_ROLE = "agent_reader"


def init_business_reader() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READER_ROLE,))
        if not cur.fetchone():
            cur.execute(f"CREATE ROLE {READER_ROLE} NOLOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {READER_ROLE}")
        # Supabase's postgres login is not a true superuser, so SET ROLE
        # requires membership; the login created the role, so it may grant it.
        cur.execute(f"GRANT {READER_ROLE} TO CURRENT_USER")
    # One grant per table, each in its own transaction, so a table that
    # doesn't exist (a dev database) doesn't take the others down with it.
    # These tables have row-level security on, which silently returns zero
    # rows to any role that isn't the owner, so the reader also needs an
    # explicit SELECT policy.
    for table in BUSINESS_TABLES:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(f"GRANT SELECT ON {table} TO {READER_ROLE}")
                cur.execute(
                    "SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = %s AND policyname = %s",
                    (table, f"{READER_ROLE}_select"),
                )
                if not cur.fetchone():
                    cur.execute(f"CREATE POLICY {READER_ROLE}_select ON {table} FOR SELECT TO {READER_ROLE} USING (true)")
        except Exception as e:
            logger.warning("No read access on %s: %s", table, e)


def describe_business_tables() -> dict[str, list[str]]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT table_name, column_name, data_type FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = ANY(%s)
                 AND column_name <> 'embedding'
               ORDER BY table_name, ordinal_position""",
            (list(BUSINESS_TABLES),),
        )
        out: dict[str, list[str]] = {}
        for table, col, typ in cur.fetchall():
            out.setdefault(table, []).append(f"{col} ({typ})")
        return out


def run_business_query(sql: str, limit: int = 50) -> list[dict]:
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        raise ValueError("One statement only.")
    if not body.lower().startswith(("select", "with")):
        raise ValueError("Only SELECT queries are allowed.")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.set_session(readonly=True, autocommit=False)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SET LOCAL ROLE {READER_ROLE}")
        cur.execute("SET LOCAL statement_timeout = 10000")
        # No parameter binding here: the model's SQL legitimately contains
        # '%' (LIKE patterns), which psycopg2 would read as placeholders.
        cur.execute(f"SELECT * FROM ({body}) AS q LIMIT {int(limit)}")
        rows = [dict(r) for r in cur.fetchall()]
        conn.rollback()
        return rows
    finally:
        conn.close()


def get_user(phone: str) -> dict | None:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return dict(row) if row else None


def save_note(user_id: str, title: str, content: str, tags: list[str] | None = None) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agent_notes (user_id, title, content, tags)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (user_id, title, content, tags or []))
        return cur.fetchone()[0]


def get_notes(user_id: str, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, title, content, tags, created_at, updated_at
            FROM agent_notes WHERE user_id = %s
            ORDER BY updated_at DESC LIMIT %s
        """, (user_id, limit))
        return [dict(r) for r in cur.fetchall()]


def search_notes(user_id: str, query: str) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, title, content, tags, created_at
            FROM agent_notes
            WHERE user_id = %s
              AND (title ILIKE %s OR content ILIKE %s)
            ORDER BY updated_at DESC LIMIT 10
        """, (user_id, f"%{query}%", f"%{query}%"))
        return [dict(r) for r in cur.fetchall()]


def save_chat_message(user_id: str, role: str, content: str, tool_calls: dict | None = None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agent_chat_history (user_id, role, content, tool_calls)
            VALUES (%s, %s, %s, %s)
        """, (user_id, role, content, json.dumps(tool_calls) if tool_calls else None))


def get_chat_history(user_id: str, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT role, content, tool_calls, created_at
            FROM agent_chat_history
            WHERE user_id = %s
            ORDER BY created_at DESC LIMIT %s
        """, (user_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows


# --- Reminders / proactive follow-ups --------------------------------------
# Aria's own table, not the legacy assistant's pa_reminders: the legacy assistant's n8n scheduler fires
# anything in pa_reminders, so writing there during the parallel run would
# send every reminder twice.

def create_reminder(user_id: str, kind: str, text: str, due_at: datetime, recurrence: str) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO agent_reminders (user_id, kind, text, due_at, recurrence)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (user_id, kind, text, due_at, recurrence),
        )
        return cur.fetchone()[0]


def list_reminders(user_id: str) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, kind, text, due_at, recurrence, status FROM agent_reminders
               WHERE user_id = %s AND status = 'pending' ORDER BY due_at LIMIT 50""",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def cancel_reminder(user_id: str, reminder_id: int) -> dict | None:
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """UPDATE agent_reminders SET status = 'cancelled', updated_at = NOW()
               WHERE id = %s AND user_id = %s AND status = 'pending'
               RETURNING id, text, due_at""",
            (reminder_id, user_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def stop_reminders(user_id: str, repeating_only: bool = True) -> int:
    """Cancel a user's pending reminders. Also called from app.py when the
    user replies "stop", so stopping never depends on the model."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE agent_reminders SET status = 'cancelled', updated_at = NOW()
               WHERE user_id = %s AND status IN ('pending', 'sending')
               AND (NOT %s OR recurrence <> 'none')""",
            (user_id, repeating_only),
        )
        return cur.rowcount


def claim_due_reminders(limit: int = 20) -> list[dict]:
    """Atomically move due reminders from pending to sending and return them.

    The claim is what stops a reminder firing twice: a second poll (or a
    second app instance) can't pick up a row that is already 'sending', and
    SKIP LOCKED keeps two pollers from waiting on each other. A row stuck in
    'sending' (the app died mid-delivery) is retried after 15 minutes, at
    most three attempts in total.
    """
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """UPDATE agent_reminders SET status = 'sending', attempts = attempts + 1, updated_at = NOW()
               WHERE id IN (
                   SELECT id FROM agent_reminders
                   WHERE attempts < 3 AND (
                       (status = 'pending' AND due_at <= NOW())
                       OR (status = 'sending' AND updated_at < NOW() - INTERVAL '15 minutes')
                   )
                   ORDER BY due_at LIMIT %s
                   FOR UPDATE SKIP LOCKED
               )
               RETURNING id, user_id, kind, text, due_at, recurrence""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def finish_reminder(reminder_id: int, next_due: datetime | None, error: str | None = None) -> None:
    """Record the outcome of a delivery attempt. This is done by code, never by
    the model: a reminder is 'sent' only once the WhatsApp message went out."""
    with get_db() as conn:
        cur = conn.cursor()
        if error:
            cur.execute(
                """UPDATE agent_reminders SET status = 'failed', last_error = %s, updated_at = NOW()
                   WHERE id = %s""",
                (error[:500], reminder_id),
            )
        elif next_due:
            # attempts counts delivery tries of *this* occurrence, so it must
            # go back to 0 here: left alone it accumulated across occurrences
            # and the claim query's `attempts < 3` silently killed every
            # repeating reminder after its third firing (seen live, Oct 2026).
            cur.execute(
                """UPDATE agent_reminders
                   SET status = 'pending', due_at = %s, attempts = 0, last_sent_at = NOW(), last_error = NULL,
                       updated_at = NOW()
                   WHERE id = %s""",
                (next_due, reminder_id),
            )
        else:
            cur.execute(
                """UPDATE agent_reminders SET status = 'sent', last_sent_at = NOW(), updated_at = NOW()
                   WHERE id = %s""",
                (reminder_id,),
            )
