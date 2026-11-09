"""Google Tasks tools, built per user (closure over the phone)."""
import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src.tools.tasks.google_tasks import (
    ADD_TASK_SCHEMA,
    COMPLETE_TASK_SCHEMA,
    LIST_TASKS_SCHEMA,
    add_task as _add_task,
    complete_task as _complete_task,
    list_tasks as _list_tasks,
)


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def build_tasks_tools(user_phone: str) -> list[SdkMcpTool]:
    @tool("list_tasks", "Show the user's Google Tasks (open ones by default) in one list, with ids.", LIST_TASKS_SCHEMA)
    async def list_tasks(args: dict) -> dict:
        return _text(await asyncio.to_thread(_list_tasks, user_phone, args.get("list_name"), bool(args.get("include_completed"))))

    @tool("add_task", "Add a task to the user's Google Tasks, optionally with notes and a due date.", ADD_TASK_SCHEMA)
    async def add_task(args: dict) -> dict:
        return _text(await asyncio.to_thread(_add_task, user_phone, args["title"], args.get("notes"), args.get("due"), args.get("list_name")))

    @tool("complete_task", "Mark a Google Task as done by its id.", COMPLETE_TASK_SCHEMA)
    async def complete_task(args: dict) -> dict:
        return _text(await asyncio.to_thread(_complete_task, user_phone, args["task_id"], args.get("list_name")))

    return [list_tasks, add_task, complete_task]


TASKS_TOOL_NAMES = ["mcp__tasks__list_tasks", "mcp__tasks__add_task", "mcp__tasks__complete_task"]
