"""Poll the Microsoft 365 intake mailbox until Brevo inbound parsing is purchased.

Requester mail already lands in contact@kynvera.net (Exchange Online). The
preferred path is Microsoft Graph (app registration + Mail.ReadWrite). IMAP is
kept as a fallback if an app password exists. Both feed
`_process_inbound_email_intake()`. When a paid Brevo inbound-parse plan is
available, leave Graph/IMAP secrets unset and use the webhook instead.
"""
from __future__ import annotations

import email
import html as html_lib
import imaplib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_TICK_ID = 'ticket_intake_mailbox_tick'
_graph_tokens: dict[str, dict] = {}

_DEFAULT_HOST = 'outlook.office365.com'
_DEFAULT_PORT = 993
_DEFAULT_FOLDER = 'INBOX'
_DEFAULT_INTERVAL = 60
_GRAPH_SCOPE = 'https://graph.microsoft.com/.default'
_GRAPH_BASE = 'https://graph.microsoft.com/v1.0'
_GRAPH_LOOKBACK_HOURS = 24


def _is_testing(app) -> bool:
    if app is not None and app.config.get('TESTING'):
        return True
    env = (os.environ.get('TESTING') or '').strip().lower()
    if env in ('1', 'true', 'yes'):
        return True
    return (os.environ.get('FLASK_ENV') or '').strip().lower() == 'testing'


def _cfg(app, key: str, default: str = '') -> str:
    val = ''
    if app is not None:
        val = str(app.config.get(key) or '').strip()
    if not val:
        val = (os.environ.get(key) or '').strip()
    return val or default


def _poll_interval(app) -> int:
    try:
        return max(30, int(_cfg(app, 'TICKET_INTAKE_IMAP_POLL_SECONDS', str(_DEFAULT_INTERVAL))))
    except ValueError:
        return _DEFAULT_INTERVAL


def graph_settings(app) -> dict | None:
    """Return Graph settings when the Entra app secret is configured."""
    tenant = _cfg(app, 'TICKET_INTAKE_GRAPH_TENANT_ID')
    client_id = _cfg(app, 'TICKET_INTAKE_GRAPH_CLIENT_ID')
    secret = _cfg(app, 'TICKET_INTAKE_GRAPH_CLIENT_SECRET')
    if not (tenant and client_id and secret):
        return None
    mailbox = (
        _cfg(app, 'TICKET_INTAKE_GRAPH_MAILBOX')
        or _cfg(app, 'TICKET_INTAKE_EMAIL')
        or 'contact@kynvera.net'
    )
    return {
        'tenant': tenant,
        'client_id': client_id,
        'secret': secret,
        'mailbox': mailbox,
        'interval': _poll_interval(app),
    }


def imap_settings(app) -> dict | None:
    """Return IMAP settings when the poller is configured; otherwise None."""
    user = _cfg(app, 'TICKET_INTAKE_IMAP_USER') or _cfg(app, 'TICKET_INTAKE_EMAIL') or 'contact@kynvera.net'
    password = _cfg(app, 'TICKET_INTAKE_IMAP_PASSWORD')
    if not password:
        return None
    try:
        port = int(_cfg(app, 'TICKET_INTAKE_IMAP_PORT', str(_DEFAULT_PORT)))
    except ValueError:
        port = _DEFAULT_PORT
    return {
        'host': _cfg(app, 'TICKET_INTAKE_IMAP_HOST', _DEFAULT_HOST),
        'port': port,
        'user': user,
        'password': password,
        'folder': _cfg(app, 'TICKET_INTAKE_IMAP_FOLDER', _DEFAULT_FOLDER),
        'interval': _poll_interval(app),
    }


def _decode_mime_header(raw) -> str:
    if raw is None:
        return ''
    if not isinstance(raw, str):
        raw = str(raw)
    parts = []
    for chunk, charset in decode_header(raw):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or 'utf-8', errors='replace'))
        else:
            parts.append(chunk)
    return ''.join(parts).strip()


def _html_to_text(value: str) -> str:
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', value)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</p>', '\n', text)
    text = re.sub(r'(?i)</div>', '\n', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_lib.unescape(text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _part_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, (bytes, bytearray)) else b''


def message_to_intake(msg: Message) -> dict:
    """Map a parsed RFC822 message into the shared intake dict."""
    from_name, from_email = parseaddr(_decode_mime_header(msg.get('From', '')))
    _to_name, to_email = parseaddr(_decode_mime_header(msg.get('To', '') or msg.get('Delivered-To', '')))

    text_body = ''
    html_body = ''
    attachments = []
    for part in msg.walk():
        disp = (part.get_content_disposition() or '').lower()
        filename = part.get_filename()
        if filename:
            filename = _decode_mime_header(filename)
        if disp == 'attachment' or (filename and disp != 'inline'):
            if filename:
                attachments.append({'name': filename, 'content': bytes(_part_bytes(part))})
            continue
        ctype = (part.get_content_type() or '').lower()
        if ctype == 'text/plain' and not text_body:
            text_body = _part_bytes(part).decode(part.get_content_charset() or 'utf-8', errors='replace')
        elif ctype == 'text/html' and not html_body:
            html_body = _part_bytes(part).decode(part.get_content_charset() or 'utf-8', errors='replace')

    body = (text_body or '').strip() or _html_to_text(html_body)
    message_id = (msg.get('Message-ID') or msg.get('Message-Id') or '').strip() or None

    return {
        'message_id': message_id,
        'from_email': (from_email or '').strip().lower() or None,
        'from_name': (from_name or '').strip() or None,
        'to_email': (to_email or '').strip() or None,
        'subject': _decode_mime_header(msg.get('Subject', '')),
        'body': body,
        'attachments': attachments,
    }


def _graph_token(settings: dict) -> str:
    now = time.time()
    cached = _graph_tokens.get(settings['client_id'])
    if cached and cached['expires_at'] > now + 60:
        return cached['token']
    resp = requests.post(
        f"https://login.microsoftonline.com/{settings['tenant']}/oauth2/v2.0/token",
        data={
            'client_id': settings['client_id'],
            'client_secret': settings['secret'],
            'scope': _GRAPH_SCOPE,
            'grant_type': 'client_credentials',
        },
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload['access_token']
    _graph_tokens[settings['client_id']] = {
        'token': token,
        'expires_at': now + int(payload.get('expires_in') or 3600),
    }
    return token


def _graph_headers(settings: dict) -> dict:
    return {'Authorization': f'Bearer {_graph_token(settings)}'}


def _intake_already_logged(message_id: str | None) -> bool:
    if not message_id:
        return False
    from app.models import TicketEmailIntake
    return TicketEmailIntake.query.filter_by(message_id=message_id).first() is not None


def _poll_graph(app) -> int:
    settings = graph_settings(app)
    if not settings:
        return 0

    from module_ticketing.routes import _process_inbound_email_intake

    mailbox = quote(settings['mailbox'])
    headers = _graph_headers(settings)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_GRAPH_LOOKBACK_HOURS)).strftime(
        '%Y-%m-%dT%H:%M:%SZ'
    )
    list_url = (
        f'{_GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages'
        f'?$filter={quote(f"receivedDateTime ge {cutoff}")}'
        f'&$select=id,internetMessageId'
        f'&$orderby={quote("receivedDateTime desc")}'
        f'&$top=25'
    )
    resp = requests.get(list_url, headers=headers, timeout=30)
    resp.raise_for_status()
    items = resp.json().get('value') or []
    processed = 0
    for item in items:
        msg_id = item.get('id')
        if not msg_id:
            continue
        graph_mid = (item.get('internetMessageId') or '').strip() or None
        if _intake_already_logged(graph_mid):
            continue
        raw_url = f'{_GRAPH_BASE}/users/{mailbox}/messages/{quote(msg_id, safe="")}/$value'
        try:
            raw_resp = requests.get(raw_url, headers=headers, timeout=30)
            raw_resp.raise_for_status()
            intake = message_to_intake(email.message_from_bytes(raw_resp.content))
            if not intake.get('to_email'):
                intake['to_email'] = settings['mailbox']
            if _intake_already_logged(intake.get('message_id')):
                continue
            _process_inbound_email_intake(intake)
            processed += 1
        except Exception:
            logger.exception('Ticket intake Graph message failed id=%s', msg_id)
            continue
        try:
            requests.patch(
                f'{_GRAPH_BASE}/users/{mailbox}/messages/{quote(msg_id, safe="")}',
                headers={**headers, 'Content-Type': 'application/json'},
                json={'isRead': True},
                timeout=20,
            ).raise_for_status()
        except Exception:
            logger.warning('Could not mark Graph message read id=%s', msg_id, exc_info=True)
    if processed:
        logger.info('Ticket intake Graph processed %s recent message(s)', processed)
    return processed


def _open_mailbox(settings: dict):
    client = imaplib.IMAP4_SSL(settings['host'], settings['port'])
    client.login(settings['user'], settings['password'])
    status, _ = client.select(settings['folder'], readonly=False)
    if status != 'OK':
        client.logout()
        raise RuntimeError(f"Could not select IMAP folder {settings['folder']}")
    return client


def _poll_imap(app) -> int:
    settings = imap_settings(app)
    if not settings:
        return 0

    from module_ticketing.routes import _process_inbound_email_intake

    processed = 0
    client = None
    try:
        client = _open_mailbox(settings)
        status, data = client.search(None, 'UNSEEN')
        if status != 'OK':
            logger.warning('Ticket intake IMAP SEARCH failed: %s', status)
            return 0
        ids = (data[0] or b'').split()
        for msg_id in ids:
            status, fetched = client.fetch(msg_id, '(RFC822)')
            if status != 'OK' or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            try:
                intake = message_to_intake(email.message_from_bytes(bytes(raw)))
                _process_inbound_email_intake(intake)
                processed += 1
            except Exception:
                logger.exception('Ticket intake IMAP message failed uid=%s', msg_id)
            try:
                client.store(msg_id, '+FLAGS', '\\Seen')
            except Exception:
                logger.warning('Could not mark IMAP message seen uid=%s', msg_id, exc_info=True)
        if processed:
            logger.info('Ticket intake IMAP processed %s unseen message(s)', processed)
        return processed
    except Exception:
        logger.exception('Ticket intake IMAP poll failed')
        return processed
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def poll_intake_mailbox(app=None) -> int:
    """Process recent intake mail into draft tickets. Graph first, then IMAP."""
    try:
        if graph_settings(app):
            return _poll_graph(app)
        if imap_settings(app):
            return _poll_imap(app)
        return 0
    except Exception:
        logger.exception('Ticket intake mailbox poll failed')
        return 0


def _run_tick(app):
    with app.app_context():
        poll_intake_mailbox(app)


def init_scheduler(app) -> None:
    """Start the mailbox poller when Graph or IMAP credentials are configured."""
    global _scheduler
    if _is_testing(app) or (app is not None and app.config.get('KYNVERA_MARKETING_ONLY')):
        return
    graph = graph_settings(app)
    imap = imap_settings(app)
    settings = graph or imap
    if not settings:
        logger.info('Ticket intake mailbox poller idle (no Graph secret or IMAP password)')
        return
    if _scheduler and _scheduler.running:
        return

    tz = ZoneInfo('Asia/Dubai')
    _scheduler = BackgroundScheduler(daemon=True, timezone=tz)
    _scheduler.add_job(
        _run_tick,
        IntervalTrigger(seconds=settings['interval'], timezone=tz),
        args=[app],
        id=_TICK_ID,
        replace_existing=True,
        next_run_time=datetime.now(tz) + timedelta(seconds=20),
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    _scheduler.start()
    if graph:
        logger.info(
            'Ticket intake Graph poller started (%s every %ss)',
            graph['mailbox'],
            graph['interval'],
        )
    else:
        logger.info(
            'Ticket intake IMAP poller started (%s@%s every %ss)',
            imap['user'],
            imap['host'],
            imap['interval'],
        )
