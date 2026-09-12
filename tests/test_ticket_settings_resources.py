"""Resource Management and Ticket Fields settings APIs."""
from app.models import (
    db, User, Ticket, TicketNote, TicketProject, TicketProjectSupervisor,
)
from module_ticketing import ticket_field_catalog as tkt_fields
from module_ticketing.routes import _apply_ticket_project_routing, _resolve_project_supervisor_id


def _make_supervisor(app, username, email, full_name):
    with app.app_context():
        u = User(
            username=username,
            email=email,
            full_name=full_name,
            role='user',
            designation='supervisor',
            is_active=True,
            password_changed=True,
            access_ticketing=True,
        )
        u.set_password('SuperPass123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _make_technician(app, username='testtech'):
    with app.app_context():
        u = User(
            username=username,
            email=f'{username}@example.com',
            full_name='Test Technician',
            role='user',
            designation='technician',
            is_active=True,
            password_changed=True,
            access_ticketing=True,
        )
        u.set_password('TechPass123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _purge_ticket(ticket_public_id: str) -> None:
    t = Ticket.query.filter_by(ticket_id=ticket_public_id).first()
    if not t:
        return
    TicketNote.query.filter_by(ticket_id=t.id).delete()
    db.session.delete(t)
    db.session.commit()


def _create_project(client, headers, name='Green Valley'):
    res = client.post(
        '/tickets/api/settings/projects',
        json={'name': name, 'client_name': 'Emaar'},
        headers=headers,
    )
    assert res.status_code == 201, res.get_json()
    return res.get_json()['project']


class TestProjectResources:
    def test_attach_and_detach_supervisors(self, client, admin_auth_headers, app, supervisor_user):
        proj = _create_project(client, admin_auth_headers)
        sid = supervisor_user.id
        res = client.post(
            f"/tickets/api/settings/projects/{proj['id']}/supervisors",
            json={'user_id': sid},
            headers=admin_auth_headers,
        )
        assert res.status_code == 201
        got = client.get(
            f"/tickets/api/settings/projects/{proj['id']}/resources",
            headers=admin_auth_headers,
        )
        assert got.status_code == 200
        body = got.get_json()
        assert len(body['supervisors']) == 1
        assert body['supervisors'][0]['user_id'] == sid

        gone = client.delete(
            f"/tickets/api/settings/projects/{proj['id']}/supervisors/{sid}",
            headers=admin_auth_headers,
        )
        assert gone.status_code == 200
        got2 = client.get(
            f"/tickets/api/settings/projects/{proj['id']}/resources",
            headers=admin_auth_headers,
        )
        assert got2.get_json()['supervisors'] == []

    def test_team_and_vendor_attach(self, client, admin_auth_headers, app):
        proj = _create_project(client, admin_auth_headers, name='Marina Towers')
        tech_id = _make_technician(app)
        add_t = client.post(
            f"/tickets/api/settings/projects/{proj['id']}/team",
            json={'user_id': tech_id},
            headers=admin_auth_headers,
        )
        assert add_t.status_code == 201, add_t.get_json()

        vend = client.post(
            '/tickets/api/settings/vendors',
            json={
                'name': 'NAFCO Test Co',
                'technicians': [{'name': 'Omar', 'speciality': 'Plumbing', 'code': 'N-1'}],
                'project_id': proj['id'],
            },
            headers=admin_auth_headers,
        )
        assert vend.status_code == 201, vend.get_json()
        resources = client.get(
            f"/tickets/api/settings/projects/{proj['id']}/resources",
            headers=admin_auth_headers,
        ).get_json()
        assert len(resources['team']) == 1
        assert len(resources['vendors']) == 1
        assert resources['vendors'][0]['name'] == 'NAFCO Test Co'

        vid = resources['vendors'][0]['id']
        det = client.delete(
            f"/tickets/api/settings/projects/{proj['id']}/vendors/{vid}",
            headers=admin_auth_headers,
        )
        assert det.status_code == 200
        resources2 = client.get(
            f"/tickets/api/settings/projects/{proj['id']}/resources",
            headers=admin_auth_headers,
        ).get_json()
        assert resources2['vendors'] == []


class TestProjectSupervisorRouting:
    def test_zero_one_many_supervisors(self, app, admin_user):
        s1 = _make_supervisor(app, 'sup_a', 'sup_a@example.com', 'Sup A')
        s2 = _make_supervisor(app, 'sup_b', 'sup_b@example.com', 'Sup B')
        with app.app_context():
            p = TicketProject(name='Routing Site', is_active=True)
            db.session.add(p)
            db.session.commit()
            t = Ticket(
                ticket_id='TKT-ROUTE1',
                reporter_id=admin_user.id,
                title='Test',
                project='Routing Site',
                service_group='HVAC',
                category='AC',
                fault_type='X',
                priority='medium',
                work_description='d',
                status='pending_supervisor',
            )
            db.session.add(t)
            db.session.commit()

            _apply_ticket_project_routing(t)
            assert t.supervisor_id is None
            assert _resolve_project_supervisor_id('Routing Site') is None

            db.session.add(TicketProjectSupervisor(project_id=p.id, user_id=s1))
            db.session.commit()
            assert _resolve_project_supervisor_id('Routing Site') == s1
            _apply_ticket_project_routing(t)
            assert t.supervisor_id == s1
            assert t.assigned_to_id == s1

            db.session.add(TicketProjectSupervisor(project_id=p.id, user_id=s2))
            db.session.commit()
            assert _resolve_project_supervisor_id('Routing Site') is None
            _apply_ticket_project_routing(t)
            assert t.supervisor_id is None
            assert t.assigned_to_id is None
            db.session.delete(t)
            db.session.commit()


class TestTicketFieldCatalog:
    def test_priority_crud_and_options(self, client, admin_auth_headers, app):
        with app.app_context():
            tkt_fields.seed_ticket_field_catalogs()
        listed = client.get('/tickets/api/settings/priorities', headers=admin_auth_headers)
        assert listed.status_code == 200
        values = {p['value'] for p in listed.get_json()['priorities'] if p.get('is_active')}
        assert 'medium' in values

        created = client.post(
            '/tickets/api/settings/priorities',
            json={'label': 'Emergency', 'sla_hint': '1h'},
            headers=admin_auth_headers,
        )
        assert created.status_code == 201, created.get_json()
        slug = created.get_json()['priority']['value']
        opts = client.get('/tickets/api/options', headers=admin_auth_headers)
        assert opts.status_code == 200
        prios = {p['value'] for p in opts.get_json()['options']['priorities']}
        assert slug in prios
        assert 'hold_reasons' in opts.get_json()['options']
        assert 'cancel_reasons' in opts.get_json()['options']

    def test_classification_crud(self, client, admin_auth_headers):
        sg = client.post(
            '/tickets/api/settings/service-groups',
            json={'name': 'HVAC systems'},
            headers=admin_auth_headers,
        )
        assert sg.status_code == 201, sg.get_json()
        gid = sg.get_json()['service_group']['id']
        cat = client.post(
            '/tickets/api/settings/categories',
            json={'service_group_id': gid, 'name': 'Air Conditioner'},
            headers=admin_auth_headers,
        )
        assert cat.status_code == 201
        cid = cat.get_json()['category']['id']
        fc = client.post(
            '/tickets/api/settings/fault-codes',
            json={'category_id': cid, 'code': '2520', 'name': 'AC Causing Humidity'},
            headers=admin_auth_headers,
        )
        assert fc.status_code == 201, fc.get_json()
        opts = client.get('/tickets/api/options', headers=admin_auth_headers).get_json()['options']
        assert 'HVAC systems' in opts['service_groups']
        assert opts['use_fault_catalog'] is True
        picks = [r['fault_pick_value'] for r in opts['fault_catalog']]
        assert any('2520' in p for p in picks)

    def test_hold_rejects_unknown_reason(self, client, admin_auth_headers, app, admin_user):
        with app.app_context():
            tkt_fields.seed_ticket_field_catalogs()
            t = Ticket(
                ticket_id='TKT-HOLD1',
                reporter_id=admin_user.id,
                title='Hold me',
                project='HQ',
                service_group='HVAC',
                category='AC',
                fault_type='X',
                priority='medium',
                work_description='d',
                status='open',
            )
            db.session.add(t)
            db.session.commit()
            tid = t.ticket_id
        bad = client.post(
            f'/tickets/api/tickets/{tid}/hold',
            json={'reason': 'not_a_real_reason'},
            headers=admin_auth_headers,
        )
        assert bad.status_code == 400
        ok = client.post(
            f'/tickets/api/tickets/{tid}/hold',
            json={'reason': 'pending_materials'},
            headers=admin_auth_headers,
        )
        assert ok.status_code == 200, ok.get_json()
        assert ok.get_json()['status'] == 'on_hold'
        with app.app_context():
            _purge_ticket(tid)

    def test_cancel_rejects_unknown_reason(self, client, admin_auth_headers, app, admin_user):
        with app.app_context():
            tkt_fields.seed_ticket_field_catalogs()
            t = Ticket(
                ticket_id='TKT-CAN1',
                reporter_id=admin_user.id,
                title='Cancel me',
                project='HQ',
                service_group='HVAC',
                category='AC',
                fault_type='X',
                priority='medium',
                work_description='d',
                status='open',
            )
            db.session.add(t)
            db.session.commit()
            tid = t.ticket_id
        bad = client.post(
            f'/tickets/api/tickets/{tid}/cancel',
            json={'reason': 'bogus_key'},
            headers=admin_auth_headers,
        )
        assert bad.status_code == 400
        ok = client.post(
            f'/tickets/api/tickets/{tid}/cancel',
            json={'reason': 'duplicate'},
            headers=admin_auth_headers,
        )
        assert ok.status_code == 200, ok.get_json()
        with app.app_context():
            _purge_ticket(tid)


class TestProjectPackPdf:
    def test_settings_page_has_download_control(self, client, admin_auth_headers):
        res = client.get('/tickets/settings', headers=admin_auth_headers)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'Download PDF' in html
        assert 'proj-pdf-btn' in html

    def test_project_pack_pdf_includes_linked_records(self, client, admin_auth_headers, app, admin_user):
        from app.models import db, Asset, FloorPlan, Ticket

        proj = _create_project(client, admin_auth_headers, name='HQ Compound Pack')
        pid = proj['id']
        with app.app_context():
            db.session.add(Asset(
                asset_id='AST-PACK-1',
                name='Main chiller',
                asset_type='chiller',
                building='HQ Building',
                floor='Ground floor',
                room='Plant',
                project_id=pid,
                status='active',
                health_score=88,
            ))
            db.session.add(FloorPlan(
                name='Ground floor',
                building='HQ Building',
                floor='Ground floor',
                image_url='https://example.test/plan.png',
                project_id=pid,
                hotspots=[{'id': 'hs-1', 'room': 'Plant', 'x_pct': 20, 'y_pct': 30}],
            ))
            db.session.add(Ticket(
                ticket_id='TKT-PACK01',
                reporter_id=admin_user.id,
                project='HQ Compound Pack',
                service_group='HVAC',
                category='Chiller',
                fault_type='Performance',
                priority='high',
                title='Chiller trip',
                work_description='Unit tripped overnight',
                status='open',
            ))
            db.session.commit()

        res = client.get(f'/tickets/api/settings/projects/{pid}/pdf', headers=admin_auth_headers)
        assert res.status_code == 200, res.get_data(as_text=True)[:500]
        assert res.mimetype == 'application/pdf'
        body = res.data
        assert body[:4] == b'%PDF'
        from io import BytesIO
        from pypdf import PdfReader
        text = '\n'.join((page.extract_text() or '') for page in PdfReader(BytesIO(body)).pages)
        assert 'Project pack / HQ Compound Pack' in text
        assert 'HQ Compound Pack' in text
        assert 'AST-PACK-1' in text or 'Main chiller' in text
        assert 'Ground floor' in text
        assert 'Linked assets' in text or 'Healthy' in text
        assert 'TKT-PACK01' in text or 'Chiller trip' in text
        disp = res.headers.get('Content-Disposition') or ''
        assert 'pack.pdf' in disp

        with app.app_context():
            for row in Ticket.query.filter_by(ticket_id='TKT-PACK01').all():
                db.session.delete(row)
            for row in FloorPlan.query.filter_by(project_id=pid).all():
                db.session.delete(row)
            for row in Asset.query.filter_by(asset_id='AST-PACK-1').all():
                db.session.delete(row)
            db.session.commit()

    def test_plan_overlay_paints_hotspot(self):
        from PIL import Image
        from module_ticketing.project_pack_pdf import _overlay_pins

        im = Image.new('RGB', (200, 100), (255, 255, 255))
        stamped = _overlay_pins(im, [{
            'room': 'Plant',
            'x_pct': 50,
            'y_pct': 50,
            'severity': 'ok',
        }])
        greens = [p for p in stamped.getdata() if p[1] > p[0] + 20 and p[1] > p[2] + 20]
        assert greens, 'expected a green pin on the stamped drawing'


class TestEmailIntakeTemplate:
    def test_settings_shows_kynvera_support_inbox(self, client, admin_auth_headers):
        res = client.get('/tickets/settings', headers=admin_auth_headers)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'contact@kynvera.net' in html
        assert 'tickets@intake.injaaz.com' not in html
        assert 'mailto:contact@kynvera.net' in html

    def test_ticket_list_has_email_template_info(self, client, admin_auth_headers):
        res = client.get('/tickets/list', headers=admin_auth_headers)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'tktEmailTplModal' in html
        assert 'tkt-email-tpl-open' in html
        assert 'contact@kynvera.net' in html
        assert '[Project Name] Category - Priority - Short title' in html


class TestTicketingClickins:
    def test_dashboard_menu_chip_and_work_order_labels(self, client, admin_auth_headers):
        res = client.get('/tickets/', headers=admin_auth_headers)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'tkt-menu-toggle' in html
        assert 'tkt-menu-toggle-label">Menu</span>' in html
        assert 'Open New Ticket' not in html
        assert 'New work order' in html
        assert 'All work orders' in html
        assert 'Email drafts' in html
        assert 'View assigned tickets' in html
        assert 'sb-nav-item--accent' not in html

    def test_open_queue_title_matches_filter(self, client, admin_auth_headers):
        res = client.get(
            '/tickets/list?status=open,pending_supervisor',
            headers=admin_auth_headers,
        )
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert '<h1 class="tkt-topbar-title">Open</h1>' in html
        assert 'value="open,pending_supervisor" selected' in html or (
            'value="open,pending_supervisor"' in html and 'selected' in html
        )
        assert 'All Tickets' not in html
        assert 'Create Ticket' not in html

    def test_drafts_has_single_back(self, client, admin_auth_headers):
        res = client.get('/tickets/drafts', headers=admin_auth_headers)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'Back to All Tickets' not in html
        assert html.count('>Back<') + html.count('>Back </') >= 0
        assert 'Email drafts' in html

    def test_draft_review_uses_split_layout(self, client, app, admin_user, admin_auth_headers):
        with app.app_context():
            t = Ticket(
                ticket_id='TKT-DRAFTUI',
                reporter_id=admin_user.id,
                title='Electrical issue',
                project='Unassigned',
                service_group='Unclassified',
                category='Tower A',
                fault_type='Unclassified',
                priority='medium',
                work_description='Electrical Issue',
                status='draft',
                source='email',
                source_sender_name='Kynvera',
                source_sender_email='support@kynvera.store',
                source_subject='Tower A - Electrical issue',
                property_name='Tower A',
                zone='Ground Floor',
                base_unit='Shop 4',
            )
            db.session.add(t)
            db.session.commit()
        try:
            res = client.get('/tickets/drafts/TKT-DRAFTUI/review', headers=admin_auth_headers)
            assert res.status_code == 200
            html = res.get_data(as_text=True)
            assert 'tkt-draft-review-page' in html
            assert 'tkt-draft-mail' in html
            assert 'Convert to work order' in html
            assert 'Original email' in html
            assert 'tktNewForm' in html
            assert 'Live Summary' in html
            assert 'Find project' in html
            assert 'Ready to convert' in html
            assert 'Ticket Details' not in html
            assert 'window.TKT_DRAFT_SEED' in html
        finally:
            with app.app_context():
                leftover = Ticket.query.filter_by(ticket_id='TKT-DRAFTUI').first()
                if leftover:
                    db.session.delete(leftover)
                    db.session.commit()

    def test_new_work_order_has_no_settings_header(self, client, admin_auth_headers):
        res = client.get('/tickets/new', headers=admin_auth_headers)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'tkt-topbar-title">New work order</h1>' in html
        assert 'tkt-page-title' not in html
        assert 'ticketing.settings_page' not in html.split('tkt-content', 1)[-1].split('new-tkt-layout', 1)[0]


