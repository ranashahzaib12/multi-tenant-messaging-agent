"""Google Tasks: the user's own task lists (the ones in Gmail's side panel and
the Tasks app). Distinct from the legacy assistant's reminders, which are timed WhatsApp
nudges; a task here is a to-do the user can tick off anywhere."""
import re
from datetime import date

from src.utils.google_auth import get_tasks_service, scope_not_granted


def _lists(service) -> list[dict]:
    return service.tasklists().list(maxResults=100).execute().get("items", [])


def _resolve_list(service, name: str | None) -> tuple[dict | None, str | None]:
    lists = _lists(service)
    if not lists:
        return None, "This Google account has no task lists yet."
    if not name:
        return lists[0], None  # Google returns the default "My Tasks" list first
    wanted = name.strip().lower()
    for tl in lists:
        if (tl.get("title") or "").strip().lower() == wanted:
            return tl, None
    for tl in lists:
        if wanted in (tl.get("title") or "").lower():
            return tl, None
    return None, f"No task list called {name!r}. Lists: " + ", ".join(tl.get("title", "?") for tl in lists)


def _due_text(task: dict) -> str:
    due = task.get("due")
    return f" (due {due[:10]})" if due else ""


def list_tasks(phone: str, list_name: str | None, include_completed: bool) -> str:
    service = get_tasks_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Tasks")
    try:
        tl, err = _resolve_list(service, list_name)
        if err:
            return err
        resp = service.tasks().list(
            tasklist=tl["id"], maxResults=100, showCompleted=bool(include_completed),
            showHidden=bool(include_completed),
        ).execute()
        others = [t.get("title", "?") for t in _lists(service) if t["id"] != tl["id"]]
    except Exception as e:
        return f"Error reading Google Tasks: {e}"
    items = resp.get("items", [])
    if not items:
        out = f"'{tl.get('title')}' has no {'tasks' if include_completed else 'open tasks'}."
    else:
        lines = []
        for t in items:
            mark = "x" if t.get("status") == "completed" else " "
            notes = f" — {t['notes'][:120]}" if t.get("notes") else ""
            lines.append(f"[{mark}] {t.get('title') or '(untitled)'}{_due_text(t)}{notes} [id: {t['id']}]")
        out = f"Tasks in '{tl.get('title')}' ({len(items)}):\n" + "\n".join(lines)
    if others:
        out += "\nOther lists: " + ", ".join(others)
    return out


def add_task(phone: str, title: str, notes: str | None, due: str | None, list_name: str | None) -> str:
    service = get_tasks_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Tasks")
    if not (title or "").strip():
        return "The task needs a title."
    body: dict = {"title": title.strip()}
    if notes:
        body["notes"] = notes
    if due:
        # Google Tasks keeps the date only; the time part is ignored.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due.strip()):
            return "Give the due date as YYYY-MM-DD."
        date.fromisoformat(due.strip())
        body["due"] = f"{due.strip()}T00:00:00.000Z"
    try:
        tl, err = _resolve_list(service, list_name)
        if err:
            return err
        task = service.tasks().insert(tasklist=tl["id"], body=body).execute()
    except Exception as e:
        return f"Error adding the task: {e}"
    return f"Added '{task.get('title')}'{_due_text(task)} to '{tl.get('title')}' [id: {task['id']}]."


def complete_task(phone: str, task_id: str, list_name: str | None) -> str:
    service = get_tasks_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Tasks")
    try:
        tl, err = _resolve_list(service, list_name)
        if err:
            return err
        task = service.tasks().patch(tasklist=tl["id"], task=task_id, body={"status": "completed"}).execute()
    except Exception as e:
        return f"Error completing the task: {e}"
    return f"Marked '{task.get('title')}' as done in '{tl.get('title')}'."


LIST_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "list_name": {"type": "string", "description": "Task list title. Omit for the default list."},
        "include_completed": {"type": "boolean", "description": "Also show completed tasks. Default false."},
    },
    "required": [],
}

ADD_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "The task."},
        "notes": {"type": "string", "description": "Optional details."},
        "due": {"type": "string", "description": "Optional due date, YYYY-MM-DD (Google Tasks keeps dates, not times)."},
        "list_name": {"type": "string", "description": "Task list title. Omit for the default list."},
    },
    "required": ["title"],
}

COMPLETE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "The [id: …] from list_tasks."},
        "list_name": {"type": "string", "description": "The list the task is in. Omit for the default list."},
    },
    "required": ["task_id"],
}
