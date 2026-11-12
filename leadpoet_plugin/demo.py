"""Fictional replies for /desk demo. No account, no network, nothing can be sent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_REPLIES = [
    ("demo-1", "Priya Raman", "Harbor Lane Partners", "question", "urgent", 12,
     "Thanks for reaching out. What does this cost for a team of five, and can we try it first?",
     "Hi Priya, happy to help. "
     "[ADD: price and trial length] Would a 15 minute call this week be useful?"),
    ("demo-2", "Marcus Oyelaran", "Pinecrest Group", "positive", "urgent", 95,
     "This is timely. Can you do Thursday afternoon?",
     "Hi Marcus, glad the timing works. Thursday afternoon is good. Does 2 PM your time suit you?"),
    ("demo-3", "Lena Fischer", "Tallgrass Associates", "negative", "normal", 1500,
     "Please remove me from your list.", None),
]


class DemoApi:
    demo = True

    def __init__(self, now: datetime | None = None):
        self._now = now or datetime.now(timezone.utc)
        self._open = {row[0]: row for row in _REPLIES}
        self.sent: list[dict] = []

    def identity(self) -> dict:
        return {"scopes": ["outreach:read", "outreach:send"]}

    def profile(self) -> dict:
        return {"offer": "A fictional product used only for this demo."}

    def suggested(self, task_id: str) -> str | None:
        row = self._open.get(task_id)
        return row[7] if row else None

    def list_replies(self) -> dict:
        tasks = []
        for task_id, name, company, label, priority, minutes, _text, _draft in self._open.values():
            tasks.append({
                "id": task_id, "enrollment_id": f"enrollment-{task_id}", "status": "open",
                "classification": label, "priority": priority,
                "received_at": (self._now - timedelta(minutes=minutes)).isoformat(),
                "contact": {"lead_id": f"lead-{task_id}", "name": name, "company": company},
                "sender": {"account_id": "demo-account", "channel": "email"},
                "sequence_stopped": False, "controlled_fulfillment_pending": False,
            })
        return {"tasks": tasks, "counts": {"to_answer": len(tasks)}}

    def conversation(self, enrollment_id: str) -> dict | None:
        row = self._open.get(enrollment_id.replace("enrollment-", ""))
        if row is None:
            return None
        return {"messages": [{"id": f"message-{row[0]}", "direction": "inbound", "text": row[6],
                              "at": (self._now - timedelta(minutes=row[5])).isoformat()}]}

    def send_reply(self, enrollment_id: str, channel: str, body: str, idempotency_key: str) -> dict:
        self.sent.append({"enrollment_id": enrollment_id, "channel": channel, "body": body,
                          "key": idempotency_key})
        self._open.pop(enrollment_id.replace("enrollment-", ""), None)
        return {"demo": True}
