"""Microsoft 365 mailbox intake → draft ticket (no live mailbox)."""
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from app.models import TicketEmailIntake, db
from module_ticketing.inbound_mailbox import (
    graph_settings,
    imap_settings,
    message_to_intake,
    poll_intake_mailbox,
)


def _rfc822(subject, body, from_addr='jane@example.com', to_addr='contact@kynvera.net', html=None):
    msg = EmailMessage()
    msg['From'] = f'Jane Doe <{from_addr}>'
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg['Message-ID'] = '<imap-test-001@example.com>'
    if html:
        msg.set_content(body)
        msg.add_alternative(html, subtype='html')
    else:
        msg.set_content(body)
    return msg


class TestMessageToIntake:
    def test_plain_text_maps_fields(self):
        msg = _rfc822(
            'Tower A - Plumbing issue',
            'Property: Tower A\nZone: Ground Floor\nUnit: Shop 4\n\nPlumbing issue\n',
        )
        intake = message_to_intake(msg)
        assert intake['from_email'] == 'jane@example.com'
        assert intake['from_name'] == 'Jane Doe'
        assert intake['to_email'] == 'contact@kynvera.net'
        assert intake['subject'] == 'Tower A - Plumbing issue'
        assert 'Property: Tower A' in intake['body']
        assert intake['message_id'] == '<imap-test-001@example.com>'
        assert intake['attachments'] == []

    def test_html_only_falls_back_to_stripped_text(self):
        msg = EmailMessage()
        msg['From'] = 'reporter@example.com'
        msg['To'] = 'contact@kynvera.net'
        msg['Subject'] = 'AC'
        msg.set_content('<p>Property: Tower A</p><p>AC is not working</p>', subtype='html')
        intake = message_to_intake(msg)
        assert 'Property: Tower A' in intake['body']
        assert 'AC is not working' in intake['body']
        assert '<p>' not in intake['body']


class TestImapSettings:
    def test_disabled_without_password(self, app):
        app.config['TICKET_INTAKE_IMAP_PASSWORD'] = ''
        assert imap_settings(app) is None

    def test_enabled_with_password(self, app):
        app.config['TICKET_INTAKE_IMAP_PASSWORD'] = 'app-password'
        app.config['TICKET_INTAKE_IMAP_USER'] = 'contact@kynvera.net'
        settings = imap_settings(app)
        assert settings['host'] == 'outlook.office365.com'
        assert settings['user'] == 'contact@kynvera.net'
        assert settings['folder'] == 'INBOX'


class TestGraphSettings:
    def test_disabled_without_secret(self, app, monkeypatch):
        monkeypatch.setenv('TICKET_INTAKE_GRAPH_CLIENT_SECRET', '')
        app.config['TICKET_INTAKE_GRAPH_TENANT_ID'] = 'tenant'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_ID'] = 'client'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_SECRET'] = ''
        assert graph_settings(app) is None

    def test_enabled_with_secret(self, app):
        app.config['TICKET_INTAKE_GRAPH_TENANT_ID'] = 'tenant-id'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_ID'] = 'client-id'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_SECRET'] = 'client-secret'
        app.config['TICKET_INTAKE_EMAIL'] = 'contact@kynvera.net'
        settings = graph_settings(app)
        assert settings['mailbox'] == 'contact@kynvera.net'
        assert settings['tenant'] == 'tenant-id'


class TestPollGraphMailbox:
    def test_recent_message_is_processed_even_if_already_read(self, app):
        app.config['TICKET_INTAKE_GRAPH_TENANT_ID'] = 'tenant-id'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_ID'] = 'client-id'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_SECRET'] = 'client-secret'
        app.config['TICKET_INTAKE_GRAPH_MAILBOX'] = 'contact@kynvera.net'
        raw = _rfc822(
            'Tower A - Plumbing issue',
            'Property: Tower A\nZone: Ground Floor\nUnit: Shop 4\n\nPlumbing issue\n',
        ).as_bytes()

        token_resp = MagicMock()
        token_resp.json.return_value = {'access_token': 'tok', 'expires_in': 3600}
        token_resp.raise_for_status.return_value = None

        list_resp = MagicMock()
        list_resp.json.return_value = {
            'value': [{'id': 'msg-1', 'internetMessageId': '<imap-test-001@example.com>'}]
        }
        list_resp.raise_for_status.return_value = None

        mime_resp = MagicMock()
        mime_resp.content = raw
        mime_resp.raise_for_status.return_value = None

        patch_resp = MagicMock()
        patch_resp.raise_for_status.return_value = None

        processed = []
        list_urls = []

        def fake_process(intake):
            processed.append(intake)

        def fake_request(method, url, **kwargs):
            if 'oauth2/v2.0/token' in url:
                return token_resp
            if url.endswith('/$value'):
                return mime_resp
            if method == 'patch':
                return patch_resp
            list_urls.append(url)
            return list_resp

        with app.app_context():
            with patch('module_ticketing.inbound_mailbox.requests.post', side_effect=lambda *a, **k: fake_request('post', a[0], **k)), \
                 patch('module_ticketing.inbound_mailbox.requests.get', side_effect=lambda *a, **k: fake_request('get', a[0], **k)), \
                 patch('module_ticketing.inbound_mailbox.requests.patch', side_effect=lambda *a, **k: fake_request('patch', a[0], **k)), \
                 patch('module_ticketing.routes._process_inbound_email_intake', side_effect=fake_process):
                count = poll_intake_mailbox(app)

        assert count == 1
        assert processed[0]['subject'] == 'Tower A - Plumbing issue'
        assert list_urls
        assert 'receivedDateTime' in list_urls[0]
        assert 'isRead' not in list_urls[0]

    def test_already_logged_message_is_skipped(self, app):
        app.config['TICKET_INTAKE_GRAPH_TENANT_ID'] = 'tenant-id'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_ID'] = 'client-id'
        app.config['TICKET_INTAKE_GRAPH_CLIENT_SECRET'] = 'client-secret'
        app.config['TICKET_INTAKE_GRAPH_MAILBOX'] = 'contact@kynvera.net'

        token_resp = MagicMock()
        token_resp.json.return_value = {'access_token': 'tok', 'expires_in': 3600}
        token_resp.raise_for_status.return_value = None

        list_resp = MagicMock()
        list_resp.json.return_value = {
            'value': [{'id': 'msg-1', 'internetMessageId': '<already-logged@example.com>'}]
        }
        list_resp.raise_for_status.return_value = None

        processed = []

        def fake_request(method, url, **kwargs):
            if 'oauth2/v2.0/token' in url:
                return token_resp
            if url.endswith('/$value'):
                raise AssertionError('already-logged messages should not be downloaded')
            return list_resp

        with app.app_context():
            db.session.add(TicketEmailIntake(
                from_email='jane@example.com',
                subject='Already on live',
                message_id='<already-logged@example.com>',
                status='processed',
            ))
            db.session.commit()
            with patch('module_ticketing.inbound_mailbox.requests.post', side_effect=lambda *a, **k: fake_request('post', a[0], **k)), \
                 patch('module_ticketing.inbound_mailbox.requests.get', side_effect=lambda *a, **k: fake_request('get', a[0], **k)), \
                 patch('module_ticketing.routes._process_inbound_email_intake', side_effect=lambda intake: processed.append(intake)):
                count = poll_intake_mailbox(app)

        assert count == 0
        assert processed == []


class TestPollIntakeMailbox:
    def test_no_password_is_noop(self, app, monkeypatch):
        monkeypatch.setenv('TICKET_INTAKE_GRAPH_CLIENT_SECRET', '')
        monkeypatch.setenv('TICKET_INTAKE_IMAP_PASSWORD', '')
        app.config['TICKET_INTAKE_IMAP_PASSWORD'] = ''
        app.config['TICKET_INTAKE_GRAPH_CLIENT_SECRET'] = ''
        with app.app_context():
            assert poll_intake_mailbox(app) == 0

    def test_unseen_message_is_processed_and_marked_seen(self, app, monkeypatch):
        monkeypatch.setenv('TICKET_INTAKE_GRAPH_TENANT_ID', '')
        monkeypatch.setenv('TICKET_INTAKE_GRAPH_CLIENT_ID', '')
        monkeypatch.setenv('TICKET_INTAKE_GRAPH_CLIENT_SECRET', '')
        app.config['TICKET_INTAKE_GRAPH_TENANT_ID'] = ''
        app.config['TICKET_INTAKE_GRAPH_CLIENT_ID'] = ''
        app.config['TICKET_INTAKE_GRAPH_CLIENT_SECRET'] = ''
        app.config['TICKET_INTAKE_IMAP_PASSWORD'] = 'app-password'
        app.config['TICKET_INTAKE_IMAP_USER'] = 'contact@kynvera.net'
        raw = _rfc822(
            'Tower A - Plumbing issue',
            'Property: Tower A\nZone: Ground Floor\nUnit: Shop 4\n\nPlumbing issue\n',
        ).as_bytes()

        client = MagicMock()
        client.search.return_value = ('OK', [b'11'])
        client.fetch.return_value = ('OK', [(b'11 (RFC822)', raw)])
        client.select.return_value = ('OK', [b'1'])

        processed = []

        def fake_process(intake):
            processed.append(intake)

        monkeypatch.setattr(
            'module_ticketing.inbound_mailbox._open_mailbox',
            lambda settings: client,
        )
        with app.app_context():
            with patch(
                'module_ticketing.routes._process_inbound_email_intake',
                side_effect=fake_process,
            ):
                count = poll_intake_mailbox(app)

        assert count == 1
        assert processed[0]['subject'] == 'Tower A - Plumbing issue'
        client.store.assert_called_once()
        assert '\\Seen' in client.store.call_args[0]
        client.logout.assert_called_once()
