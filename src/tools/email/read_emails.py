from src.utils.google_auth import get_gmail_service, not_connected


def read_emails(phone: str, query: str, max_results: int) -> str:
    service = get_gmail_service(phone)
    if not service:
        return not_connected("Gmail", phone)

    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        messages = result.get("messages", [])

        if not messages:
            return f"No emails found for query: {query}"

        lines = []
        for msg_meta in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_meta["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            snippet = msg.get("snippet", "")

            lines.append(
                f"- From: {headers.get('From', '?')}\n"
                f"  Subject: {headers.get('Subject', '(no subject)')}\n"
                f"  Date: {headers.get('Date', '?')}\n"
                f"  Preview: {snippet[:200]}\n"
                f"  [id: {msg_meta['id']}]"
            )

        return f"Found {len(messages)} email(s):\n\n" + "\n\n".join(lines)

    except Exception as e:
        return f"Error reading emails: {e}"


READ_EMAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Gmail search query. Examples: 'is:unread', 'from:boss@example.com', "
                "'subject:invoice'. Default 'is:unread'."
            ),
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of emails to return. Default 5.",
        },
    },
    "required": [],
}
