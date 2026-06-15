"""
SwitchToAI Outreach Approval Bot
---------------------------------
Two modes:

1. SCHEDULER (cron: 0 8 * * *)
   - Pulls Notion CRM
   - Finds contacts due for E2 or E3
   - Sends Telegram message with draft email
   - Stores pending approval in Supabase

2. BOT LISTENER (long-poll, runs continuously as a separate process)
   - Waits for your Telegram replies
   - 'send'          -> fires email via Brevo, updates Notion
   - 'skip'          -> marks skipped in Supabase, no email sent
   - Any other text  -> treats it as an edited email body, asks you to confirm

Run scheduler:  python agent.py --schedule
Run bot:        python agent.py --bot
On Railway:     two services, one for each mode
"""

import argparse
import base64
import logging
import os
import sys
import time
from datetime import date, datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIG  (Railway env vars)
# ---------------------------------------------------------------------------
NOTION_TOKEN     = os.environ["NOTION_TOKEN"]
NOTION_DB_ID     = os.environ.get("NOTION_DB_ID", "3765462bed8480e9bd86fde3dcb6b6de")
BREVO_API_KEY    = os.environ["BREVO_API_KEY"]
SENDER_EMAIL     = os.environ.get("SENDER_EMAIL", "founder@switchtoai.ai")
SENDER_NAME      = "Ahmed M"
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8619666343:AAH4qhw_sgaTVr0gUlIzb2CkZMTauPmbwUg")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5858773467")
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]

E1_TO_E2_DAYS    = 5
E2_TO_E3_DAYS    = 5

NOTION_API       = "https://api.notion.com/v1"
NOTION_VER       = "2022-06-28"
TELEGRAM_API     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
BREVO_API        = "https://api.brevo.com/v3/smtp/email"
SUPABASE_TABLE   = "outreach_pending"


# ---------------------------------------------------------------------------
# EMAIL TEMPLATES
# ---------------------------------------------------------------------------

def build_e2(first_name: str, company: str, notes: str, trigger: str, e1_subject: str) -> tuple[str, str]:
    callback = _trigger_callback(notes, trigger, company)
    subject  = f"Re: {e1_subject}"
    body = f"""Hi {first_name},

Following up on my note from last week.

{callback}

We mapped the admin load for a similar agency recently and found 8 hours a week being lost across lead response, follow-up chasing, and listing admin. Full breakdown here if useful: switchtoai.ai/case-study

Worth 30 minutes to see if the numbers stack up the same way at {company}?

Ahmed M
AI Consultant, SwitchToAI
switchtoai.ai

Not relevant? Reply with 'unsubscribe' and I'll remove you immediately."""
    return subject, body


def build_e3(first_name: str, company: str, e1_subject: str) -> tuple[str, str]:
    subject = f"Re: {e1_subject}"
    body = f"""Hi {first_name},

Last note from me on this.

Timing may just not be right — completely understood. If it ever shifts, the free AI tool check at switchtoai.ai takes 60 seconds and gives a specific starting point for your setup at {company}.

Good luck with the rest of Q3.

Ahmed M
AI Consultant, SwitchToAI
switchtoai.ai

Not relevant? Reply with 'unsubscribe' and I'll remove you immediately."""
    return subject, body


def _trigger_callback(notes: str, trigger: str, company: str) -> str:
    combined = (trigger + " " + notes).lower()
    HIRING   = ["hir", "bring", "recruit", "negotiator", "new role", "new member", "rental agent", "new staff"]
    LISTING  = ["listing", "rightmove", "zoopla", "portal", "stock", "properties"]
    GROWTH   = ["growth", "expand", "new office", "branch", "scaling", "opened"]
    WEEKEND  = ["weekend", "response time", "slow response", "missed lead"]
    CAPACITY = ["busy", "capacity", "understaffed", "stretched", "overwhelmed"]

    if any(w in combined for w in HIRING):
        return "The hiring angle I mentioned still applies — automation handles the admin ceiling faster and cheaper than a new headcount."
    if any(w in combined for w in LISTING):
        return "With the listing activity you're running, the manual side of that pipeline adds up quickly."
    if any(w in combined for w in GROWTH):
        return "Growth usually means admin load scales faster than the team does — that's exactly the window this is designed for."
    if any(w in combined for w in WEEKEND):
        return "The weekend response gap I flagged is one of the fastest to close — usually under a day to set up."
    if any(w in combined for w in CAPACITY):
        return "The capacity pressure I mentioned is exactly where automation makes the biggest dent first."
    return f"The question I raised about {company} still stands."


def _derive_subject(notes: str, trigger: str, company: str) -> str:
    import re
    if notes:
        m = re.search(r"subject:\s*(.+)", notes, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    combined = (trigger + " " + notes).lower()
    if any(w in combined for w in ["hir", "bring", "recruit", "negotiator"]):
        return "Before you hire"
    if any(w in combined for w in ["listing", "rightmove", "zoopla"]):
        return f"{company} — listing volume"
    if any(w in combined for w in ["growth", "expand"]):
        return f"{company} — growth"
    return f"Quick question, {company}"


# ---------------------------------------------------------------------------
# NOTION HELPERS
# ---------------------------------------------------------------------------

def _nheaders() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def get_all_rows() -> list[dict]:
    url, rows, cursor = f"{NOTION_API}/databases/{NOTION_DB_ID}/query", [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(url, headers=_nheaders(), json=payload)
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    log.info(f"Fetched {len(rows)} Notion rows")
    return rows


def _text(props, key):
    prop = props.get(key, {})
    t    = prop.get("type")
    # Handle email type (Notion stores as plain string under "email" key)
    if t == "email":
        return prop.get("email") or ""
    items = prop.get(t, []) if t in ("title", "rich_text") else []
    return "".join(i.get("plain_text", "") for i in items).strip()


def _email(props, key):
    """Dedicated helper for Notion email-type properties."""
    prop = props.get(key, {})
    return prop.get("email") or ""


def _date(props, key):
    prop = props.get(key, {})
    if prop.get("type") == "date" and prop.get("date"):
        raw = prop["date"].get("start")
        if raw:
            return datetime.fromisoformat(raw).date()
    return None


def _select(props, key):
    prop = props.get(key, {})
    t    = prop.get("type")
    # Notion "status" type: {"type": "status", "status": {"name": "..."}}
    # Notion "select" type: {"type": "select", "select": {"name": "..."}}
    for k in ("select", "status"):
        inner = prop.get(k)
        if inner and isinstance(inner, dict):
            return inner.get("name", "")
    return ""


def _checkbox(props, key):
    return bool(props.get(key, {}).get("checkbox", False))


def update_notion_row(page_id: str, new_status: str, date_field: str) -> None:
    today = datetime.today().strftime("%Y-%m-%d")
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=_nheaders(),
        json={"properties": {
            "Status":   {"select": {"name": new_status}},
            date_field: {"date": {"start": today}},
        }}
    )
    r.raise_for_status()
    log.info(f"Notion updated: {page_id} → {new_status}")


# ---------------------------------------------------------------------------
# SUPABASE HELPERS
# Stores pending approvals so the bot listener can retrieve them.
#
# Table: outreach_pending
# Columns:
#   id            uuid default gen_random_uuid() primary key
#   page_id       text
#   company       text
#   contact       text
#   to_email      text
#   action        text          -- 'E2' or 'E3'
#   subject       text
#   body          text
#   status        text          -- 'pending' | 'sent' | 'skipped' | 'edited'
#   telegram_msg_id  bigint     -- message ID of the Telegram notification
#   created_at    timestamptz default now()
# ---------------------------------------------------------------------------

def _sheaders() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def save_pending(page_id, company, contact, to_email, action, subject, body, tg_msg_id) -> str:
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=_sheaders(),
        json={
            "page_id": page_id, "company": company, "contact": contact,
            "to_email": to_email, "action": action, "subject": subject,
            "body": body, "status": "pending", "telegram_msg_id": tg_msg_id,
        }
    )
    r.raise_for_status()
    row_id = r.json()[0]["id"]
    log.info(f"Saved pending: {row_id} ({company} {action})")
    return row_id


def get_pending_by_msg_id(tg_msg_id: int) -> dict | None:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=_sheaders(),
        params={"telegram_msg_id": f"eq.{tg_msg_id}", "status": "eq.pending", "limit": 1}
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def get_pending_by_id(row_id: str) -> dict | None:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=_sheaders(),
        params={"id": f"eq.{row_id}", "limit": 1}
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def update_pending_status(row_id: str, status: str) -> None:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers={**_sheaders(), "Prefer": "return=minimal"},
        params={"id": f"eq.{row_id}"},
        json={"status": status}
    )
    r.raise_for_status()


def update_pending_body(row_id: str, new_body: str) -> None:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers={**_sheaders(), "Prefer": "return=minimal"},
        params={"id": f"eq.{row_id}"},
        json={"body": new_body, "status": "edited"}
    )
    r.raise_for_status()


def get_edited_awaiting_confirm(tg_msg_id: int) -> dict | None:
    """Find a row in 'edited' state linked to this message thread."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=_sheaders(),
        params={"telegram_msg_id": f"eq.{tg_msg_id}", "status": "eq.edited", "limit": 1}
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# BREVO SENDER
# ---------------------------------------------------------------------------

def send_via_brevo(to_email: str, to_name: str, subject: str, body: str) -> None:
    payload = {
        "sender":   {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to":       [{"email": to_email, "name": to_name}],
        "subject":  subject,
        "textContent": body,
    }
    r = requests.post(
        BREVO_API,
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload
    )
    r.raise_for_status()
    log.info(f"Brevo sent → {to_email} | subject: {subject}")


# ---------------------------------------------------------------------------
# TELEGRAM HELPERS
# ---------------------------------------------------------------------------

def tg_send(text: str, parse_mode: str = "Markdown") -> int:
    """Send message, return message_id."""
    r = requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
    })
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def tg_reply(chat_id: int, reply_to_id: int, text: str) -> None:
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id":             chat_id,
        "text":                text,
        "reply_to_message_id": reply_to_id,
        "parse_mode":          "Markdown",
    })


def tg_get_updates(offset: int = 0) -> list[dict]:
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params={
        "offset":  offset,
        "timeout": 30,
    }, timeout=35)
    r.raise_for_status()
    return r.json().get("result", [])


# ---------------------------------------------------------------------------
# SCHEDULER — finds due contacts and sends Telegram drafts
# ---------------------------------------------------------------------------

def identify_due(rows: list[dict]) -> list[dict]:
    today, due = date.today(), []
    for row in rows:
        props   = row.get("properties", {})
        company = _text(props, "Company")
        contact = _text(props, "Contact")
        email   = _email(props, "Email")
        status  = _select(props, "Status")
        notes   = _text(props, "Notes")
        trigger = _text(props, "Trigger")
        replied = _checkbox(props, "Reply?")
        e1      = _date(props, "Email 1 sent")
        e2      = _date(props, "Email 2 sent")
        e3      = _date(props, "Email 3 sent")

        if replied or not company:
            continue
        if not email:
            log.warning(f"SKIP (no email): {company} / {contact}")
            continue

        first_name = contact.split()[0] if contact else company
        e1_subject = _derive_subject(notes, trigger, company)
        action     = None

        if status == "Sent E1" and e1 and not e2:
            if (today - e1).days >= E1_TO_E2_DAYS:
                action = "E2"
        elif status == "Sent E2" and e2 and not e3:
            if (today - e2).days >= E2_TO_E3_DAYS:
                action = "E3"

        if action:
            due.append({
                "page_id":    row["id"],
                "company":    company,
                "first_name": first_name,
                "contact":    contact,
                "email":      email,
                "notes":      notes,
                "trigger":    trigger,
                "e1_subject": e1_subject,
                "action":     action,
            })
    log.info(f"Due today: {len(due)}")
    return due


def run_scheduler():
    log.info("=== Scheduler run ===")
    rows = get_all_rows()
    due  = identify_due(rows)

    if not due:
        log.info("Nothing due. Done.")
        return

    for item in due:
        fn, co, em = item["first_name"], item["company"], item["email"]
        action     = item["action"]

        if action == "E2":
            subject, body = build_e2(fn, co, item["notes"], item["trigger"], item["e1_subject"])
        else:
            subject, body = build_e3(fn, co, item["e1_subject"])

        # Send Telegram notification with the draft
        msg_text = (
            f"*{action} due — {co}*\n"
            f"To: {em}\n"
            f"Subject: {subject}\n"
            f"{'─' * 30}\n"
            f"{body}\n"
            f"{'─' * 30}\n"
            f"Reply *send* to fire this email.\n"
            f"Reply *skip* to ignore.\n"
            f"Reply with any edited text to replace the body before sending."
        )

        try:
            tg_msg_id = tg_send(msg_text)
            save_pending(
                item["page_id"], co, item["contact"], em,
                action, subject, body, tg_msg_id
            )
            log.info(f"Telegram draft sent for {co} {action} (msg_id={tg_msg_id})")
        except Exception as e:
            log.error(f"Failed to notify for {co}: {e}")


# ---------------------------------------------------------------------------
# BOT LISTENER — processes your Telegram replies
# ---------------------------------------------------------------------------

def run_bot():
    log.info("=== Bot listener starting ===")
    offset = 0

    while True:
        try:
            updates = tg_get_updates(offset)
        except Exception as e:
            log.error(f"getUpdates error: {e}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            msg    = update.get("message", {})
            if not msg:
                continue

            chat_id    = msg["chat"]["id"]
            text       = msg.get("text", "").strip()
            reply_to   = msg.get("reply_to_message", {})
            replied_id = reply_to.get("message_id") if reply_to else None

            if str(chat_id) != str(TELEGRAM_CHAT_ID):
                continue  # ignore messages from anyone else

            if not replied_id:
                continue  # only process replies to bot messages

            # Find the pending row this reply refers to
            pending = get_pending_by_msg_id(replied_id)

            # Also check if this is a confirmation reply to an edited draft
            if not pending:
                edited = get_edited_awaiting_confirm(replied_id)
                if edited:
                    handle_edit_confirm(edited, text, chat_id, replied_id)
                continue

            handle_reply(pending, text, chat_id, replied_id)

        time.sleep(1)


def handle_reply(pending: dict, text: str, chat_id: int, replied_id: int):
    row_id  = pending["id"]
    company = pending["company"]
    action  = pending["action"]
    cmd     = text.lower().strip()

    if cmd == "send":
        # Fire the email as-is
        try:
            send_via_brevo(pending["to_email"], pending["contact"], pending["subject"], pending["body"])
            notion_status, date_field = ("Sent E2", "Email 2 sent") if action == "E2" else ("Sent E3", "Email 3 sent")
            update_notion_row(pending["page_id"], notion_status, date_field)
            update_pending_status(row_id, "sent")
            tg_reply(chat_id, replied_id, f"Sent to {pending['to_email']}. Notion updated to *{notion_status}*.")
        except Exception as e:
            tg_reply(chat_id, replied_id, f"Send failed: {e}")
            log.error(f"Brevo send failed for {company}: {e}")

    elif cmd == "skip":
        update_pending_status(row_id, "skipped")
        tg_reply(chat_id, replied_id, f"Skipped. Notion not updated — {company} stays at current status.")

    else:
        # Treat the reply as an edited body
        update_pending_body(row_id, text)
        preview = text[:300] + ("..." if len(text) > 300 else "")
        tg_reply(
            chat_id, replied_id,
            f"Updated body saved.\n\n*Preview:*\n{preview}\n\n"
            f"Reply *send* to fire this edited version, or *skip* to discard."
        )


def handle_edit_confirm(edited: dict, text: str, chat_id: int, replied_id: int):
    cmd = text.lower().strip()
    if cmd == "send":
        try:
            send_via_brevo(edited["to_email"], edited["contact"], edited["subject"], edited["body"])
            notion_status, date_field = ("Sent E2", "Email 2 sent") if edited["action"] == "E2" else ("Sent E3", "Email 3 sent")
            update_notion_row(edited["page_id"], notion_status, date_field)
            update_pending_status(edited["id"], "sent")
            tg_reply(chat_id, replied_id, f"Edited version sent to {edited['to_email']}. Notion updated.")
        except Exception as e:
            tg_reply(chat_id, replied_id, f"Send failed: {e}")
    elif cmd == "skip":
        update_pending_status(edited["id"], "skipped")
        tg_reply(chat_id, replied_id, "Skipped.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", action="store_true", help="Run the scheduler (cron mode)")
    parser.add_argument("--bot",      action="store_true", help="Run the Telegram bot listener")
    args = parser.parse_args()

    if args.schedule:
        run_scheduler()
    elif args.bot:
        run_bot()
    else:
        print("Usage: python agent.py --schedule | --bot")
        sys.exit(1)
