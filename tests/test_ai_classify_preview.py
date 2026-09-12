"""
Tests for the AI complaint-classification suggestion (module_ai_triage.triage.
classify_ticket + the /tickets/api/tickets/classify-preview route).

This exists because email-intake drafts never get a Service Group / Category /
Fault code (_process_inbound_email_intake hardcodes 'Unclassified') — the
reviewer used to have to search the fault catalog by hand. classify_ticket()
asks Claude to pick a fault_code_id out of the existing catalog; the important
thing to verify is that a ungrounded/hallucinated id from the model is
rejected rather than silently trusted, since nothing downstream re-validates it
before the UI offers to apply it to the ticket.
"""
import pytest

from module_ai_triage import triage as triage_mod

FAKE_CATALOG = [
    {
        'id': 2513, 'category_id': 10, 'catalog_id': 2513,
        'fault_code': '2513', 'fault_code_name': 'AC is not working',
        'fault_category': 'Air Conditioner', 'service_group': 'HVAC systems',
        'fault_pick_value': '2513: AC is not working',
    },
    {
        'id': 1180, 'category_id': 11, 'catalog_id': 1180,
        'fault_code': '1180', 'fault_code_name': 'Carpentry-Others',
        'fault_category': 'Carpentry', 'service_group': 'Carpenter works',
        'fault_pick_value': '1180: Carpentry-Others',
    },
]


class TestClassifyTicket:
    def test_llm_disabled_returns_error(self, monkeypatch):
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        monkeypatch.setattr(triage_mod, '_fault_catalog_rows', lambda: FAKE_CATALOG)
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: False)
        result = triage_mod.classify_ticket({'title': 'AC is not working'})
        assert result['success'] is False
        assert result['suggestion'] is None

    def test_missing_title_and_description_returns_error(self, monkeypatch):
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        result = triage_mod.classify_ticket({})
        assert result['success'] is False
        assert 'title or work_description' in result['error']

    def test_empty_catalog_returns_error(self, monkeypatch):
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        monkeypatch.setattr(triage_mod, '_fault_catalog_rows', lambda: [])
        result = triage_mod.classify_ticket({'title': 'AC is not working'})
        assert result['success'] is False
        assert 'catalog' in result['error'].lower()

    def test_valid_match_is_returned(self, monkeypatch):
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        monkeypatch.setattr(triage_mod, '_fault_catalog_rows', lambda: FAKE_CATALOG)
        monkeypatch.setattr(
            triage_mod, 'generate_structured',
            lambda *a, **k: {'fault_code_id': 2513, 'reasoning': 'Matches verbatim.'},
        )
        result = triage_mod.classify_ticket({
            'title': 'Tower A - AC is not working',
            'work_description': 'AC is not working',
        })
        assert result['success'] is True
        s = result['suggestion']
        assert s['fault_code_id'] == 2513
        assert s['service_group'] == 'HVAC systems'
        assert s['category'] == 'Air Conditioner'
        assert s['fault_pick_value'] == '2513: AC is not working'
        assert s['reasoning'] == 'Matches verbatim.'

    def test_hallucinated_id_not_in_catalog_is_rejected(self, monkeypatch):
        """The model returning an id outside the catalog must not be trusted —
        it should come back as no-match, not as a fabricated suggestion."""
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        monkeypatch.setattr(triage_mod, '_fault_catalog_rows', lambda: FAKE_CATALOG)
        monkeypatch.setattr(
            triage_mod, 'generate_structured',
            lambda *a, **k: {'fault_code_id': 999999, 'reasoning': 'made up'},
        )
        result = triage_mod.classify_ticket({'title': 'AC is not working'})
        assert result['success'] is True
        assert result['suggestion'] is None

    def test_null_id_means_no_match(self, monkeypatch):
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        monkeypatch.setattr(triage_mod, '_fault_catalog_rows', lambda: FAKE_CATALOG)
        monkeypatch.setattr(
            triage_mod, 'generate_structured',
            lambda *a, **k: {'fault_code_id': None, 'reasoning': 'Nothing close enough.'},
        )
        result = triage_mod.classify_ticket({'title': 'Something totally unrelated'})
        assert result['success'] is True
        assert result['suggestion'] is None
        assert result['reasoning'] == 'Nothing close enough.'

    def test_llm_error_is_surfaced_not_raised(self, monkeypatch):
        monkeypatch.setattr(triage_mod, 'is_llm_enabled', lambda: True)
        monkeypatch.setattr(triage_mod, '_fault_catalog_rows', lambda: FAKE_CATALOG)

        def boom(*a, **k):
            raise triage_mod.StructuredLLMError('model unavailable')

        monkeypatch.setattr(triage_mod, 'generate_structured', boom)
        result = triage_mod.classify_ticket({'title': 'AC is not working'})
        assert result['success'] is False
        assert 'model unavailable' in result['error']


class TestClassifyPreviewRoute:
    def test_requires_auth(self, client):
        resp = client.post('/tickets/api/tickets/classify-preview', json={'title': 'x'})
        assert resp.status_code in (401, 422)

    def test_requires_title_or_description(self, client, admin_auth_headers):
        resp = client.post(
            '/tickets/api/tickets/classify-preview',
            headers=admin_auth_headers,
            json={},
        )
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False

    def test_happy_path_returns_suggestion(self, client, admin_auth_headers, monkeypatch):
        monkeypatch.setattr(
            triage_mod, 'classify_ticket',
            lambda payload: {
                'success': True,
                'suggestion': {
                    'fault_code_id': 2513,
                    'service_group': 'HVAC systems',
                    'category': 'Air Conditioner',
                    'fault_code': '2513',
                    'fault_code_name': 'AC is not working',
                    'fault_pick_value': '2513: AC is not working',
                    'reasoning': 'Matches verbatim.',
                },
            },
        )
        resp = client.post(
            '/tickets/api/tickets/classify-preview',
            headers=admin_auth_headers,
            json={'title': 'Tower A - AC is not working', 'work_description': 'AC is not working'},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['suggestion']['fault_pick_value'] == '2513: AC is not working'
