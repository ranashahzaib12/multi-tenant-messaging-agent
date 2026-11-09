from datetime import datetime, timedelta, timezone

from src.utils.google_auth import get_calendar_service, not_connected


def get_calendar_events(phone: str, days_ahead: int, query: str | None) -> str:
    service = get_calendar_service(phone)
    if not service:
        return not_connected("Google Calendar", phone)

    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)

    try:
        params = {
            "calendarId": "primary",
            "timeMin": now.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 20,
        }
        if query:
            params["q"] = query

        result = service.events().list(**params).execute()
        events = result.get("items", [])

        if not events:
            return f"No events found in the next {days_ahead} days."

        lines = []
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            end = event["end"].get("dateTime", event["end"].get("date"))
            summary = event.get("summary", "(No title)")
            location = event.get("location", "")
            event_id = event.get("id", "")

            line = f"- {summary}: {start} to {end}"
            if location:
                line += f" at {location}"
            attendees = [a.get("email") for a in event.get("attendees", []) if a.get("email") and not a.get("self")]
            if attendees:
                line += f" with {', '.join(attendees)}"
            if event.get("hangoutLink"):
                line += f" (Meet: {event['hangoutLink']})"
            line += f" [id: {event_id}]"
            lines.append(line)

        return f"Found {len(events)} event(s):\n" + "\n".join(lines)

    except Exception as e:
        return f"Error fetching calendar events: {e}"


GET_EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "days_ahead": {
            "type": "integer",
            "description": "Number of days ahead to fetch events for. Default 7.",
        },
        "query": {
            "type": "string",
            "description": "Optional search query to filter events by summary or description.",
        },
    },
    "required": [],
}
