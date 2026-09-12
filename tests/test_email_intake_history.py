"""
Tests for the "Email intake history" section on Tickets -> Email drafts
(/tickets/drafts): every TicketEmailIntake row, bucketed into
pending / converted / discarded / failed, with date-range + status + search
filters. Added because the page previously only ever showed tickets still in
status='draft' — once a draft was converted or discarded it just vanished,
leaving no record of how many emails came in vs. what happened to them.
"""
from datetime import datetime, timedelta

import pytest

from app.models import db, Ticket, TicketEmailIntake


def _make_intake(from_email, subject, status='processed', ticket=None, received_at=None):
    row = TicketEmailIntake(
        from_email=from_email,
        from_name=None,
        to_email='contact@kynvera.net',
        subject=subject,
        raw_body='body',
        message_id=f'{subject}-{from_email}',
        status=status,
        ticket_id=ticket.id if ticket else None,
        received_at=received_at or datetime.utcnow(),
    )
    db.session.add(row)
    return row


def _make_ticket(ticket_id, status, reporter_id):
    t = Ticket(
        ticket_id=ticket_id, reporter_id=reporter_id, title='t', project='Unassigned',
        service_group='Unclassified', category='Unclassified', fault_type='Unclassified',
        priority='medium', work_description='d', status=status, source='email',
    )
    db.session.add(t)
    db.session.flush()
    return t


class TestEmailIntakeHistoryBucketing:
    @pytest.fixture
    def seeded(self, app, admin_user):
        with app.app_context():
            admin_id = admin_user.id
            pending_t = _make_ticket('TKT-HIST-PEND', 'draft', admin_id)
            converted_t = _make_ticket('TKT-HIST-CONV', 'pending_supervisor', admin_id)
            discarded_t = _make_ticket('TKT-HIST-DISC', 'cancelled', admin_id)

            _make_intake('pending@example.com', 'Pending one', ticket=pending_t)
            _make_intake('converted@example.com', 'Converted one', ticket=converted_t)
            _make_intake('discarded@example.com', 'Discarded one', ticket=discarded_t)
            _make_intake('failed@example.com', 'Could not parse', status='error', ticket=None)
            db.session.commit()
        yield
        with app.app_context():
            TicketEmailIntake.query.filter(
                TicketEmailIntake.from_email.in_([
                    'pending@example.com', 'converted@example.com',
                    'discarded@example.com', 'failed@example.com',
                ])
            ).delete(synchronize_session=False)
            Ticket.query.filter(Ticket.ticket_id.in_(
                ['TKT-HIST-PEND', 'TKT-HIST-CONV', 'TKT-HIST-DISC']
            )).delete(synchronize_session=False)
            db.session.commit()

    def test_counts_bucket_by_ticket_status_not_intake_status(self, client, admin_auth_headers, seeded):
        resp = client.get('/tickets/drafts', headers=admin_auth_headers)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'Pending one' in html
        assert 'Converted one' in html
        assert 'Discarded one' in html
        assert 'Could not parse' in html
        assert 'Pending review' in html
        assert 'Discarded' in html
        assert 'Failed to parse' in html

    def test_status_filter_narrows_to_one_bucket(self, client, admin_auth_headers, seeded):
        resp = client.get('/tickets/drafts?status=converted', headers=admin_auth_headers)
        html = resp.get_data(as_text=True)
        assert 'Converted one' in html
        assert 'Pending one' not in html
        assert 'Discarded one' not in html
        assert 'Could not parse' not in html

    def test_search_filters_by_sender_and_subject(self, client, admin_auth_headers, seeded):
        resp = client.get('/tickets/drafts?q=discarded%40example.com', headers=admin_auth_headers)
        html = resp.get_data(as_text=True)
        assert 'Discarded one' in html
        assert 'Converted one' not in html
        assert 'Pending one' not in html

    def test_date_range_excludes_out_of_range_rows(self, client, admin_auth_headers, app, admin_user):
        with app.app_context():
            old_t = _make_ticket('TKT-HIST-OLD', 'pending_supervisor', admin_user.id)
            _make_intake(
                'old@example.com', 'Very old email', ticket=old_t,
                received_at=datetime.utcnow() - timedelta(days=90),
            )
            db.session.commit()
        try:
            today = datetime.utcnow().date().isoformat()
            resp = client.get(
                f'/tickets/drafts?date_from={today}&date_to={today}',
                headers=admin_auth_headers,
            )
            html = resp.get_data(as_text=True)
            assert 'Very old email' not in html
        finally:
            with app.app_context():
                TicketEmailIntake.query.filter_by(from_email='old@example.com').delete()
                Ticket.query.filter_by(ticket_id='TKT-HIST-OLD').delete()
                db.session.commit()

    def test_requires_draft_review_access(self, client, auth_headers):
        resp = client.get('/tickets/drafts', headers=auth_headers)
        assert resp.status_code in (302, 403)
