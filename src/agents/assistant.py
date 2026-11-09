import asyncio
import logging
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timezone

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
)

from src.prompts.manager import MANAGER_PROMPT
from src.prompts.email_agent import EMAIL_AGENT_PROMPT
from src.prompts.calendar_agent import CALENDAR_AGENT_PROMPT
from src.prompts.notes_agent import NOTES_AGENT_PROMPT
from src.prompts.research_agent import RESEARCH_AGENT_PROMPT
from src.prompts.documents_agent import DOCUMENTS_AGENT_PROMPT
from src.prompts.reminders_agent import REMINDERS_AGENT_PROMPT
from src.prompts.browser_agent import BROWSER_AGENT_PROMPT
from src.prompts.memory_agent import MEMORY_AGENT_PROMPT
from src.prompts.data_agent import DATA_AGENT_PROMPT
from src.prompts.images_agent import IMAGES_AGENT_PROMPT
from src.prompts.drive_agent import DRIVE_AGENT_PROMPT
from src.prompts.tasks_agent import TASKS_AGENT_PROMPT
from src.tools.images import image_tools, IMAGE_TOOL_NAMES
from src.tools.drive import build_drive_tools, DRIVE_TOOL_NAMES
from src.tools.workspace import build_workspace_tools, WORKSPACE_TOOL_NAMES
from src.tools.tasks import build_tasks_tools, TASKS_TOOL_NAMES
from src.tools.contacts import build_contacts_tools, CONTACTS_TOOL_NAMES
from src.config import BUSINESS_DATA_PHONES, CLAUDE_MODEL
from src.tools.calendar import build_calendar_tools, CALENDAR_TOOL_NAMES
from src.tools.email import build_email_tools, EMAIL_TOOL_NAMES
from src.tools.notes import build_notes_tools, NOTES_TOOL_NAMES
from src.tools.documents import build_document_tools, DOCUMENT_TOOL_NAMES
from src.tools.reminders import build_reminder_tools, REMINDER_TOOL_NAMES
from src.tools.browser import build_browser_tools, BROWSER_TOOL_NAMES
from src.tools.memory import build_memory_tools, MEMORY_TOOL_NAMES
from src.tools.data import build_data_tools, DATA_TOOL_NAMES, BUSINESS_TOOL_NAMES
from src.tools.memory.profile import format_profile
from src import database as db

logger = logging.getLogger(__name__)

USER_TIMEZONE = "Asia/Singapore"

# Returned instead of raising so a WhatsApp turn always gets a reply; the
# scheduler compares against it to tell a failed turn from a real answer.
AGENT_ERROR_REPLY = "Something went wrong processing your message. Please try again."

# Web research uses Claude Code's built-in tools (Anthropic's server-side web
# search, billed on the same API key) instead of a third-party search API.
RESEARCH_TOOL_NAMES = ["WebSearch", "WebFetch"]

ALL_TOOL_NAMES = (
    CALENDAR_TOOL_NAMES + EMAIL_TOOL_NAMES + NOTES_TOOL_NAMES + RESEARCH_TOOL_NAMES + DOCUMENT_TOOL_NAMES
    + REMINDER_TOOL_NAMES + BROWSER_TOOL_NAMES + MEMORY_TOOL_NAMES + DATA_TOOL_NAMES + BUSINESS_TOOL_NAMES
    + IMAGE_TOOL_NAMES + DRIVE_TOOL_NAMES + WORKSPACE_TOOL_NAMES + TASKS_TOOL_NAMES + CONTACTS_TOOL_NAMES
)


async def _user_turn_with_images(text: str, images: list[tuple[str, str]]) -> AsyncIterator[dict]:
    """The SDK's streaming-input form of a single user message, which is the
    only way to send image blocks through query()."""
    content: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
        for media_type, data in images
    ]
    content.append({"type": "text", "text": text})
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


class PersonalAssistant:
    """Wires a Claude Agent SDK manager agent with ten specialist subagents.

    Authenticates with the ANTHROPIC_API_KEY in the environment (metered API
    usage); a deployed product may not run on a claude.ai subscription login.
    """

    def __init__(self):
        # Per-user session continuity: phone -> Claude Agent SDK session_id.
        # In-memory only; lost on restart. Fine for an MVP, but a production
        # deployment should persist this map (e.g. in pa_users.profile).
        self._sessions: dict[str, str] = {}
        # One turn at a time per user: the reminder scheduler can start a turn
        # while the user is mid-conversation, and two resumes of one session at
        # once corrupt its history.
        self._locks: dict[str, asyncio.Lock] = {}

    def _build_options(self, user_phone: str, profile: dict) -> ClaudeAgentOptions:
        current_time = datetime.now(timezone.utc).isoformat()
        user_timezone = profile.get("timezone") or USER_TIMEZONE
        format_kwargs = {
            "current_time": current_time,
            "user_timezone": user_timezone,
            "user_profile": format_profile(profile) or "(nothing yet — learn as you go)",
        }

        # Never rely on ambient session state (e.g. a CLAUDE_CODE_SESSION_ID
        # left in the process environment) to decide which conversation a
        # message belongs to — that risks one user's turn landing in another
        # user's session. Always assign and pin an explicit per-phone
        # session id instead: a fresh one on first contact, resumed after.
        # The id is also kept in the user's profile, and the transcripts live
        # under CLAUDE_CONFIG_DIR on the persistent volume, so a redeploy no
        # longer wipes everyone's conversation memory.
        existing_session_id = self._sessions.get(user_phone) or profile.get("session_id")
        if existing_session_id:
            resume_id = existing_session_id
            new_session_id = None
        else:
            resume_id = None
            new_session_id = str(uuid.uuid4())
        self._sessions[user_phone] = existing_session_id or new_session_id

        calendar_tools = build_calendar_tools(user_phone)
        email_tools = build_email_tools(user_phone)
        notes_tools = build_notes_tools(user_phone)
        document_tools = build_document_tools(user_phone, profile.get("legacy_user_id"))
        reminder_tools = build_reminder_tools(user_phone, user_timezone)
        browser_tools = build_browser_tools(user_phone)
        memory_tools = build_memory_tools(user_phone)
        owner = user_phone in BUSINESS_DATA_PHONES
        data_tools = build_data_tools(user_phone, profile.get("legacy_user_id"), owner)
        drive_tools = build_drive_tools(user_phone)
        workspace_tools = build_workspace_tools(user_phone)
        tasks_tools = build_tasks_tools(user_phone)
        contacts_tools = build_contacts_tools(user_phone)

        mcp_servers = {
            "calendar": create_sdk_mcp_server("calendar", tools=calendar_tools),
            "email": create_sdk_mcp_server("email", tools=email_tools),
            "notes": create_sdk_mcp_server("notes", tools=notes_tools),
            "documents": create_sdk_mcp_server("documents", tools=document_tools),
            "reminders": create_sdk_mcp_server("reminders", tools=reminder_tools),
            "browser": create_sdk_mcp_server("browser", tools=browser_tools),
            "memory": create_sdk_mcp_server("memory", tools=memory_tools),
            "data": create_sdk_mcp_server("data", tools=data_tools),
            "images": create_sdk_mcp_server("images", tools=image_tools),
            "drive": create_sdk_mcp_server("drive", tools=drive_tools),
            "workspace": create_sdk_mcp_server("workspace", tools=workspace_tools),
            "tasks": create_sdk_mcp_server("tasks", tools=tasks_tools),
            "contacts": create_sdk_mcp_server("contacts", tools=contacts_tools),
        }

        agents = {
            "calendar_agent": AgentDefinition(
                description="Handles Google Calendar: viewing, creating, and deleting events.",
                prompt=CALENDAR_AGENT_PROMPT.format(**format_kwargs),
                tools=CALENDAR_TOOL_NAMES + CONTACTS_TOOL_NAMES,
            ),
            "email_agent": AgentDefinition(
                description="Handles Gmail: reading, sending and organising emails (archive, labels, trash), reading attachments and saving them to Drive.",
                prompt=EMAIL_AGENT_PROMPT.format(current_time=current_time),
                tools=EMAIL_TOOL_NAMES + CONTACTS_TOOL_NAMES + ["mcp__calendar__google_connect_link"],
            ),
            "notes_agent": AgentDefinition(
                description="Saves, lists, and searches the user's personal notes.",
                prompt=NOTES_AGENT_PROMPT.format(current_time=current_time),
                tools=NOTES_TOOL_NAMES,
            ),
            "research_agent": AgentDefinition(
                description="Searches the web for current information and facts.",
                prompt=RESEARCH_AGENT_PROMPT.format(current_time=current_time),
                tools=RESEARCH_TOOL_NAMES,
            ),
            "documents_agent": AgentDefinition(
                description="Finds answers in, lists, and deletes the documents (PDF/Word/text) the user has sent.",
                prompt=DOCUMENTS_AGENT_PROMPT.format(current_time=current_time),
                tools=DOCUMENT_TOOL_NAMES,
            ),
            "reminders_agent": AgentDefinition(
                description="Schedules, lists and cancels reminders and timed follow-ups for the user.",
                prompt=REMINDERS_AGENT_PROMPT.format(**format_kwargs),
                tools=REMINDER_TOOL_NAMES,
            ),
            "data_agent": AgentDefinition(
                description="Answers from the database: the user's earlier records (health log, personas, past reminders, past chats) and, for owners, the business tables (leads, products, knowledge base).",
                prompt=DATA_AGENT_PROMPT.format(**format_kwargs),
                tools=DATA_TOOL_NAMES + (BUSINESS_TOOL_NAMES if owner else []),
            ),
            "drive_agent": AgentDefinition(
                description="Google Drive, Docs and Sheets: finds and reads files, creates a Doc or Sheet, appends to one, saves the file the user just sent into Drive.",
                prompt=DRIVE_AGENT_PROMPT.format(current_time=current_time),
                tools=DRIVE_TOOL_NAMES + WORKSPACE_TOOL_NAMES + ["mcp__calendar__google_connect_link"],
            ),
            "tasks_agent": AgentDefinition(
                description="The user's Google Tasks lists: show, add and tick off to-dos.",
                prompt=TASKS_AGENT_PROMPT.format(**format_kwargs),
                tools=TASKS_TOOL_NAMES + ["mcp__calendar__google_connect_link"],
            ),
            "images_agent": AgentDefinition(
                description="Makes pictures: generates an image from a description, or draws a line/bar chart from numbers.",
                prompt=IMAGES_AGENT_PROMPT.format(current_time=current_time),
                tools=IMAGE_TOOL_NAMES,
            ),
            "memory_agent": AgentDefinition(
                description="Saves, updates and recalls what is known about the user: name, age, weight, family, preferences, timezone.",
                prompt=MEMORY_AGENT_PROMPT.format(current_time=current_time),
                tools=MEMORY_TOOL_NAMES,
            ),
            "browser_agent": AgentDefinition(
                description="Makes guest bookings on public websites with a headless browser: restaurants, appointments, slots. No logins or payments.",
                prompt=BROWSER_AGENT_PROMPT.format(**format_kwargs),
                tools=BROWSER_TOOL_NAMES,
            ),
        }

        return ClaudeAgentOptions(
            model=CLAUDE_MODEL,  # subagents inherit it
            system_prompt=MANAGER_PROMPT.format(**format_kwargs),
            mcp_servers=mcp_servers,
            allowed_tools=ALL_TOOL_NAMES,
            # Claude Code's file tools never prompt, so "dontAsk" alone did not
            # stop them: the assistant once read its own source on the server
            # and quoted line numbers and env var names to a user. Remove every
            # built-in that touches the filesystem or shell; the MCP tools above
            # plus WebSearch/WebFetch (research_agent) and Task (subagents) remain.
            disallowed_tools=[
                "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead",
                "Glob", "Grep", "LS", "Bash", "BashOutput", "KillShell", "TodoWrite", "Skill",
            ],
            agents=agents,
            permission_mode="dontAsk",
            resume=resume_id,
            session_id=new_session_id,
            # Defense in depth: strip any ambient session id from this
            # process's own environment so it can never override the
            # explicit resume/session_id above.
            env={"CLAUDE_CODE_SESSION_ID": ""},
        )

    async def ainvoke(self, message: str, user_phone: str, images: list[tuple[str, str]] | None = None) -> str:
        """images: (media_type, base64) photos the user attached; they go to
        the model as image blocks alongside the text."""
        lock = self._locks.setdefault(user_phone, asyncio.Lock())
        async with lock:
            try:
                profile = await asyncio.to_thread(db.get_profile, user_phone)
            except Exception:
                logger.debug("Profile load skipped (no DB)", exc_info=True)
                profile = {}
            options = self._build_options(user_phone, profile)
            result_text = ""

            prompt: str | AsyncIterable[dict] = message
            if images:
                prompt = _user_turn_with_images(message, images)

            for attempt in (1, 2):
                try:
                    async for msg in query(prompt=prompt, options=options):
                        if isinstance(msg, ResultMessage):
                            if msg.result:
                                result_text = msg.result
                            self._sessions[user_phone] = msg.session_id
                            # One line per turn so spend per message and the
                            # model actually used can be read off the logs.
                            u = msg.usage or {}
                            logger.info(
                                "Turn cost: $%.4f, %d API round(s), in=%s cache_read=%s cache_write=%s out=%s, models=%s",
                                msg.total_cost_usd or 0.0, msg.num_turns or 0,
                                u.get("input_tokens"), u.get("cache_read_input_tokens"),
                                u.get("cache_creation_input_tokens"), u.get("output_tokens"),
                                ",".join((msg.model_usage or {}).keys()) or "?",
                            )
                    break
                except Exception as e:
                    # A stored session id whose transcript is gone (volume
                    # wiped, id from another host) makes resume fail; drop it
                    # and answer in a fresh session rather than failing the turn.
                    if attempt == 1 and options.resume:
                        logger.warning("Resume of session for %s failed (%s); starting fresh", user_phone, e)
                        self._sessions.pop(user_phone, None)
                        profile = dict(profile, session_id=None)
                        options = self._build_options(user_phone, profile)
                        if images:
                            prompt = _user_turn_with_images(message, images)
                        continue
                    logger.error("Assistant error for %s: %s", user_phone, e)
                    return AGENT_ERROR_REPLY

            session_id = self._sessions.get(user_phone)
            if session_id and session_id != profile.get("session_id"):
                try:
                    await asyncio.to_thread(db.set_session_id, user_phone, session_id)
                except Exception:
                    logger.debug("Session id persist skipped (no DB)", exc_info=True)

            return result_text or "No response generated."

    def invoke(self, message: str, user_phone: str) -> str:
        """Sync convenience wrapper. Do not call from inside a running event loop."""
        return asyncio.run(self.ainvoke(message, user_phone))
