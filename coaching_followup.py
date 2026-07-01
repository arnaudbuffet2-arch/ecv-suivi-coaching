"""
Suivi automatique post-coaching (J+15).

Usage :
  python coaching_followup.py --scan [--depuis YYYY-MM-DD]
  python coaching_followup.py --send
  python coaching_followup.py --test EMAIL [--prenom PRENOM]
  python coaching_followup.py --print-template
"""
import argparse
import base64
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from pathlib import Path

LOG_FILE = Path(__file__).parent / "coaching_followup.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from google_auth import get_google_credentials

PENDING_FILE = Path(__file__).parent / "pending_followups.json"
DOC_ID = "1Nn0Oa5ks_bKBg0r_RuV-HbSwTIQqhf94B-22s1m4pP8"
SEND_AFTER_DAYS = 15    # envoi automatique 15 jours après la séance
MAX_PER_DAY = 5         # anti-spam : max 5 créations et max 5 envois par jour
EMAIL_SUBJECT = "J'ai un cadeau pour te remercier de ta confiance… 🎁🎵"
SCAN_WINDOW_DAYS = 60


# ── Pending file ────────────────────────────────────────────────────────────

def load_pending():
    if not PENDING_FILE.exists():
        return []
    return json.loads(PENDING_FILE.read_text(encoding="utf-8-sig"))


def save_pending(entries):
    PENDING_FILE.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _email_blocked(pending, email, current_entry):
    """True si cet email a déjà reçu l'email (sent non annulé) ou a un autre brouillon en attente."""
    email_lower = email.lower()
    for e in pending:
        if e is current_entry:
            continue
        if e["email_to"].lower() != email_lower:
            continue
        if e.get("sent") and not e.get("cancelled"):
            return True
        if e.get("draft_id") and not e.get("sent"):
            return True
    return False


# ── Calendar ─────────────────────────────────────────────────────────────────

def is_calendly_event(event):
    description = event.get("description", "") or ""
    if "calendly" in description.lower():
        return True
    creator_email = (event.get("creator") or {}).get("email", "")
    if "calendly" in creator_email.lower():
        return True
    organizer_email = (event.get("organizer") or {}).get("email", "")
    if "calendly" in organizer_email.lower():
        return True
    location = event.get("location", "") or ""
    if "calendly" in location.lower():
        return True
    return False


def extract_client_info(event):
    attendees = event.get("attendees", [])
    email = None
    for att in attendees:
        if not att.get("self") and not att.get("organizer"):
            email = att.get("email")
            break

    title = event.get("summary", "")
    if " et " in title:
        prenom = title.split(" et ")[0].strip().split()[0]
    elif "(" in title:
        prenom = title.split("(")[0].strip().split()[0]
    else:
        prenom = title.strip().split()[0]

    return email, prenom


def _migrate_v2(pending):
    """Migre les entrées v1 : send_date était la date du coaching, maintenant c'est coaching + 15j."""
    changed = 0
    for e in pending:
        if "event_date" not in e and not e.get("sent"):
            e["event_date"] = e["send_date"]
            e["send_date"] = (
                datetime.fromisoformat(e["event_date"]) + timedelta(days=SEND_AFTER_DAYS)
            ).strftime("%Y-%m-%d")
            changed += 1
    if changed:
        logging.info(f"Migration v2 : {changed} entrée(s) migrée(s) — send_date = event_date + {SEND_AFTER_DAYS}j.")
    return changed > 0


def scan(creds, depuis=None, debug=False):
    service = build("calendar", "v3", credentials=creds)
    now = datetime.now(timezone.utc)
    time_min = depuis or (now - timedelta(days=SCAN_WINDOW_DAYS))
    time_max = now + timedelta(days=SCAN_WINDOW_DAYS)  # inclut les réservations futures

    events = []
    page_token = None
    while True:
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if debug:
        logging.info(f"DEBUG scan: {len(events)} événement(s) trouvé(s) dans la fenêtre [{time_min.date()} → {time_max.date()}]")

    pending = load_pending()
    known_ids = {e["event_id"] for e in pending}
    known_emails = {e["email_to"].lower() for e in pending}

    stats = {"non_calendly": 0, "deja_connu": 0, "sans_email": 0, "email_doublon": 0}
    nouveaux = 0
    for event in events:
        if not is_calendly_event(event):
            stats["non_calendly"] += 1
            if debug:
                date_key = event["start"].get("dateTime", event["start"].get("date", "?"))[:10]
                creator = (event.get("creator") or {}).get("email", "—")
                desc_snippet = (event.get("description", "") or "")[:80].replace("\n", " ")
                logging.info(f"DEBUG non-Calendly: '{event.get('summary','?')}' le {date_key} | creator={creator} | desc={desc_snippet!r}")
            continue
        event_id = event["id"]
        if event_id in known_ids:
            stats["deja_connu"] += 1
            continue

        email, prenom = extract_client_info(event)
        if not email:
            stats["sans_email"] += 1
            if debug:
                logging.info(f"DEBUG sans_email: '{event.get('summary', '?')}' le {event['start'].get('dateTime', event['start'].get('date', '?'))[:10]}")
            continue
        if email.lower() in known_emails:
            stats["email_doublon"] += 1
            continue

        date_str = event["start"].get("dateTime") or event["start"].get("date", "")
        event_date = datetime.fromisoformat(date_str)
        event_date_str = event_date.strftime("%Y-%m-%d")
        send_date = (event_date + timedelta(days=SEND_AFTER_DAYS)).strftime("%Y-%m-%d")

        pending.append({
            "event_id": event_id,
            "event_date": event_date_str,
            "send_date": send_date,
            "email_to": email,
            "prenom": prenom,
            "sent": False,
            "added_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })
        known_emails.add(email.lower())
        nouveaux += 1

    save_pending(pending)
    if debug:
        logging.info(f"DEBUG résumé: non-Calendly={stats['non_calendly']} déjà_connu={stats['deja_connu']} sans_email={stats['sans_email']} email_doublon={stats['email_doublon']} nouveaux={nouveaux}")
    if nouveaux > 0:
        logging.info(f"NOUVEAU: {nouveaux} coaching(s) ajouté(s) au suivi.")


# ── Template (Google Doc → HTML) ─────────────────────────────────────────────

def _extract_plain(element, parts):
    if "paragraph" in element:
        for pe in element["paragraph"].get("elements", []):
            if "textRun" in pe:
                parts.append(pe["textRun"].get("content", ""))
    elif "table" in element:
        for row in element["table"].get("tableRows", []):
            for cell in row.get("tableCells", []):
                for ce in cell.get("content", []):
                    _extract_plain(ce, parts)


def get_template(creds, doc_id):
    """Returns (subject, html_body) from the Google Doc."""
    # Plain text via Docs API → extract subject
    docs_svc = build("docs", "v1", credentials=creds)
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    parts = []
    for block in doc.get("body", {}).get("content", []):
        _extract_plain(block, parts)
    plain = "".join(parts)

    # HTML export via Drive API
    drive_svc = build("drive", "v3", credentials=creds)
    html_bytes = drive_svc.files().export(fileId=doc_id, mimeType="text/html").execute()
    html = html_bytes.decode("utf-8")

    # Extract CSS + body content
    style_match = re.search(r'(<style[^>]*>.*?</style>)', html, re.DOTALL | re.IGNORECASE)
    styles = style_match.group(1) if style_match else ""

    body_tag = re.search(r'<body[^>]*>', html, re.IGNORECASE)
    body_start = body_tag.end() if body_tag else 0
    body_end_m = re.search(r'</body>', html, re.IGNORECASE)
    body_end = body_end_m.start() if body_end_m else len(html)

    html_body = styles + html[body_start:body_end]
    return EMAIL_SUBJECT, html_body


# ── Send ──────────────────────────────────────────────────────────────────────

def _extract_inline_images(html):
    """Replace base64 images with CID references. Returns (html, [(cid, subtype, bytes)])."""
    images = []

    def replace(match):
        subtype = match.group(1)  # jpeg, png, gif…
        raw = base64.b64decode(match.group(2))
        cid = f"img{len(images)}"
        images.append((cid, subtype, raw))
        return f'src="cid:{cid}"'

    html = re.sub(r'src="data:image/([^;]+);base64,([^"]+)"', replace, html)
    return html, images


def _enhance_html(html):
    """Post-process HTML for better email rendering and CTR."""

    # 1. Preheader: invisible preview text shown after subject in inbox
    preheader = (
        '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
        'Votre code exclusif GIFTEMILE — 19€ au lieu de 43€ 🎁'
        '</div>'
    )
    html = re.sub(r'(<body[^>]*>)', r'\1' + preheader, html, count=1, flags=re.IGNORECASE)

    # 2. Highlight prices (43 € struck, 19 € bold green)
    html = re.sub(
        r'\b43\s*€\b',
        '<span style="text-decoration:line-through;color:#888888;">43&nbsp;€</span>',
        html,
    )
    html = re.sub(
        r'\b19\s*€\b',
        '<span style="color:#2e7d32;font-weight:bold;font-size:1.2em;">19&nbsp;€</span>',
        html,
    )

    # 3. CTA: replace entire paragraph containing the CTA link with a proper button
    def make_button(match):
        href_match = re.search(r'href="([^"]+)"', match.group(0))
        href = href_match.group(1) if href_match else "#"
        return (
            f'<table cellpadding="0" cellspacing="0" border="0" width="100%"'
            f' style="margin:24px 0;">'
            f'<tr><td align="center">'
            f'<a href="{href}" target="_blank"'
            f' style="display:inline-block;background-color:#c8a234;color:#1a1a1a;'
            f'font-family:Arial,sans-serif;font-size:17px;font-weight:bold;'
            f'text-decoration:none;padding:16px 40px;border-radius:6px;'
            f'letter-spacing:0.5px;">JE PROFITE DE L\'OFFRE EXCLUSIVE</a>'
            f'</td></tr></table>'
        )

    html = re.sub(
        r'<p[^>]*>(?:[^<]|<(?!/?p))*?JE PROFITE(?:[^<]|<(?!/?p))*?</p>',
        make_button,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 4b. Add top spacing before specific headings by inserting a spacer row before their <p>
    spacer = '<p style="margin:0;padding:0;line-height:1em;">&nbsp;</p>'
    for phrase in ["J&rsquo;ai un cadeau pour toi", "Pour te remercier de ta confiance"]:
        html = re.sub(
            r'(<p\b[^>]*>[^<]*(?:<span[^>]*>)?[^<]*' + re.escape(phrase) + r')',
            spacer + r'\1',
            html, count=1, flags=re.DOTALL,
        )

    # 4c. Remove empty/spacer paragraphs (no img, no visible text)
    def is_empty_p(match):
        inner = match.group(1)
        if '<img' in inner:
            return match.group(0)  # keep image paragraphs
        stripped = re.sub(r'<[^>]+>', '', inner).replace('&nbsp;', '').strip()
        return '' if not stripped else match.group(0)

    html = re.sub(r'<p[^>]*>(.*?)</p>', is_empty_p, html, flags=re.DOTALL)

    # 4. RGPD footer — désabonnement obligatoire
    rgpd_footer = (
        '<div style="margin-top:32px;padding-top:16px;border-top:1px solid #dddddd;'
        'text-align:center;font-family:Arial,sans-serif;font-size:11px;color:#999999;">'
        'Vous recevez cet email car vous avez suivi un cours avec Emile Coach Vocal.<br>'
        'Pour ne plus recevoir ces emails, répondez avec "STOP" à ce message.'
        '</div>'
    )
    html = re.sub(r'(</body>)', rgpd_footer + r'\1', html, count=1, flags=re.IGNORECASE)

    return html


def _html_to_plain(html):
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _build_message(email_to, subject, html_body, prenom):
    body = html_body.replace("{{prenom}}", prenom).replace("{{PRENOM}}", prenom.upper())
    body = _enhance_html(body)
    body, images = _extract_inline_images(body)

    # multipart/related wraps HTML + inline images
    msg = MIMEMultipart("related")
    msg["To"] = email_to
    msg["Subject"] = subject

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(_html_to_plain(body), "plain", "utf-8"))
    alternative.attach(MIMEText(body, "html", "utf-8"))
    msg.attach(alternative)

    for cid, subtype, raw in images:
        img = MIMEImage(raw, _subtype=subtype)
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline")
        msg.attach(img)

    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def bulk_schedule(creds):
    """One-shot : supprime les brouillons existants et crée tous les suivis en attente,
    envoi échelonné 5/jour à partir de J+15."""
    pending = load_pending()
    gmail = build("gmail", "v1", credentials=creds)

    unsent = [e for e in pending if not e["sent"]]

    # Dédupliquer par email (garder la première occurrence)
    seen_emails: set = set()
    deduped = []
    skipped = 0
    for e in unsent:
        key = e["email_to"].lower()
        if key in seen_emails:
            skipped += 1
        else:
            seen_emails.add(key)
            deduped.append(e)
    if skipped:
        logging.info(f"{skipped} doublon(s) d'email ignoré(s).")
    unsent = deduped

    # Supprimer les brouillons Gmail existants
    deleted = 0
    for entry in unsent:
        if entry.get("draft_id"):
            try:
                gmail.users().drafts().delete(userId="me", id=entry["draft_id"]).execute()
                deleted += 1
            except Exception:
                pass
            entry.pop("draft_id", None)
            entry.pop("draft_created", None)

    if deleted:
        logging.info(f"{deleted} brouillon(s) existant(s) supprimé(s) de Gmail.")

    # Créer tous les brouillons avec dates échelonnées
    subject, html_body = get_template(creds, DOC_ID)
    today = datetime.now().date()
    created = 0

    for i, entry in enumerate(unsent):
        batch_offset = i // 5
        draft_created = (today + timedelta(days=batch_offset)).strftime("%Y-%m-%d")
        raw = _build_message(entry["email_to"], subject, html_body, entry.get("prenom", ""))
        result = gmail.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        entry["draft_id"] = result["id"]
        entry["draft_created"] = draft_created
        created += 1

    save_pending(pending)
    first_send = today + timedelta(days=SEND_AFTER_DAYS)
    last_send = today + timedelta(days=(len(unsent) - 1) // 5 + SEND_AFTER_DAYS)
    logging.info(f"{created} brouillon(s) créé(s). Envois du {first_send.strftime('%d/%m/%Y')} au {last_send.strftime('%d/%m/%Y')}, 5 par jour.")


def create_drafts_due(creds):
    """Crée des brouillons Gmail pour tous les suivis sans brouillon."""
    pending = load_pending()
    today = datetime.now().strftime("%Y-%m-%d")
    to_draft = [e for e in pending if not e["sent"] and not e.get("draft_id")]
    to_draft = to_draft[:MAX_PER_DAY]

    if not to_draft:
        logging.info("Aucun brouillon à créer.")
        return

    subject, html_body = get_template(creds, DOC_ID)
    gmail = build("gmail", "v1", credentials=creds)

    count = 0
    blocked = 0
    for entry in to_draft:
        if _email_blocked(pending, entry["email_to"], entry):
            entry["sent"] = True
            entry["cancelled"] = True
            blocked += 1
            continue
        raw = _build_message(entry["email_to"], subject, html_body, entry.get("prenom", ""))
        result = gmail.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        entry["draft_id"] = result["id"]
        entry["draft_created"] = today
        count += 1

    save_pending(pending)
    if blocked:
        logging.info(f"{blocked} brouillon(s) bloqué(s) — email déjà reçu ou brouillon existant.")
    logging.info(f"{count} brouillon(s) créé(s) — envoi à la date programmée si non supprimé.")


def auto_send_drafts(creds):
    """Envoie automatiquement les brouillons non supprimés après le délai de grâce."""
    pending = load_pending()
    today = datetime.now()

    to_check = [
        e for e in pending
        if not e["sent"] and e.get("draft_id") and e.get("draft_created")
    ]

    if not to_check:
        logging.info("Aucun brouillon en attente d'envoi automatique.")
        return

    gmail = build("gmail", "v1", credentials=creds)
    sent_count = 0
    cancelled_count = 0
    today_str = today.strftime("%Y-%m-%d")
    send_budget = MAX_PER_DAY

    for entry in to_check:
        if entry.get("send_date", "") > today_str:
            continue

        if send_budget <= 0:
            break

        # Vérifier si le brouillon existe encore
        try:
            gmail.users().drafts().get(userId="me", id=entry["draft_id"]).execute()
        except Exception:
            # Brouillon supprimé par l'utilisateur → annulé
            entry["sent"] = True
            entry["cancelled"] = True
            cancelled_count += 1
            continue

        # Vérifier qu'aucun autre envoi n'a déjà eu lieu pour cet email
        if _email_blocked(pending, entry["email_to"], entry):
            entry["sent"] = True
            entry["cancelled"] = True
            cancelled_count += 1
            continue

        # Brouillon toujours là → envoi automatique
        try:
            gmail.users().drafts().send(
                userId="me", body={"id": entry["draft_id"]}
            ).execute()
            entry["sent"] = True
            sent_count += 1
            send_budget -= 1
        except Exception as exc:
            logging.warning(f"Envoi échoué pour {entry.get('email_to', '?')} : {exc} — brouillon annulé.")
            entry["sent"] = True
            entry["cancelled"] = True
            cancelled_count += 1

    save_pending(pending)
    if sent_count:
        logging.info(f"{sent_count} email(s) envoyé(s) automatiquement.")
    if cancelled_count:
        logging.info(f"{cancelled_count} brouillon(s) annulé(s) (supprimés manuellement).")
    if not sent_count and not cancelled_count:
        logging.info("Aucun brouillon arrivé à échéance aujourd'hui.")


def send_due(creds):
    """Envoie directement sans passer par les brouillons (usage manuel uniquement)."""
    pending = load_pending()
    today = datetime.now().strftime("%Y-%m-%d")
    to_send = [e for e in pending if not e["sent"] and e["send_date"] <= today]

    if not to_send:
        print("Aucun email à envoyer aujourd'hui.")
        return

    subject, html_body = get_template(creds, DOC_ID)
    gmail = build("gmail", "v1", credentials=creds)

    sent_count = 0
    for entry in to_send:
        raw = _build_message(entry["email_to"], subject, html_body, entry.get("prenom", ""))
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        entry["sent"] = True
        sent_count += 1

    save_pending(pending)
    print(f"{sent_count} email(s) envoyé(s).")


def send_test(creds, email_to, prenom="Arnaud"):
    subject, html_body = get_template(creds, DOC_ID)
    raw = _build_message(email_to, f"[TEST] {subject}", html_body, prenom)
    gmail = build("gmail", "v1", credentials=creds)
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email de test envoyé — Objet : {subject}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="scan + draft + auto-send en un seul appel (usage quotidien)")
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--debug", action="store_true", help="Logs détaillés sur le scan")
    parser.add_argument("--bulk-schedule", action="store_true", help="One-shot stock : crée tous les brouillons, envoi 5/jour à J+15")
    parser.add_argument("--draft", action="store_true", help="Crée des brouillons Gmail à J+15")
    parser.add_argument("--auto-send", action="store_true", help="Envoie les brouillons non supprimés")
    parser.add_argument("--send", action="store_true", help="Envoie directement sans brouillon")
    parser.add_argument("--test", type=str, metavar="EMAIL")
    parser.add_argument("--prenom", type=str, default="Arnaud")
    parser.add_argument("--depuis", type=str, help="YYYY-MM-DD")
    parser.add_argument("--print-template", action="store_true")
    args = parser.parse_args()

    creds = get_google_credentials()

    if args.all:
        pending = load_pending()
        if _migrate_v2(pending):
            save_pending(pending)
        scan(creds, debug=args.debug)
        create_drafts_due(creds)
        auto_send_drafts(creds)
    elif args.bulk_schedule:
        bulk_schedule(creds)
    elif args.scan:
        depuis = None
        if args.depuis:
            depuis = datetime.fromisoformat(args.depuis).replace(tzinfo=timezone.utc)
        scan(creds, depuis, debug=args.debug)
    elif args.draft:
        create_drafts_due(creds)
    elif args.auto_send:
        auto_send_drafts(creds)
    elif args.send:
        send_due(creds)
    elif args.test:
        send_test(creds, args.test, args.prenom)
    elif args.print_template:
        subject, html_body = get_template(creds, DOC_ID)
        print(f"Objet : {subject}")
        print(f"HTML body : {len(html_body)} caractères")
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("ERREUR FATALE : %s", e)
        sys.exit(1)
