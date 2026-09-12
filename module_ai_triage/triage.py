"""
Intelligent work-order triage via Claude structured JSON.

triage_ticket() returns suggestions only — never auto-applies assignment.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.models import (
    db, User, Ticket, Asset, Technician, TicketSupervisorTeam, TicketTriageLog,
)
from module_assistant.llm import generate_structured, StructuredLLMError, is_llm_enabled

logger = logging.getLogger(__name__)

ALLOWED_PRIORITIES = frozenset({'low', 'medium', 'high', 'critical'})
SLA_MIN, SLA_MAX = 1, 72

TRIAGE_SCHEMA = {
    'required': ['priority', 'sla_hours', 'required_parts', 'reasoning'],
    'properties': {
        'priority': str,
        'sla_hours': int,
        'technician_id': (int, type(None)),
        'required_parts': list,
        'reasoning': str,
    },
}

SYSTEM_PROMPT = (
    'You are an FM work-order triage assistant for Kynvera. '
    'Given a ticket description, location, optional asset history, and a roster of '
    'assignable technicians (User accounts), suggest priority, SLA hours, technician_id, '
    'and required_parts. '
    'priority must be one of: low, medium, high, critical. '
    'sla_hours must be an integer between 1 and 72. '
    'technician_id must be one of the provided candidate user ids, or null if none fit. '
    'required_parts is a list of short part/material names (may be empty). '
    'reasoning is one short sentence.'
)


def _asset_history_summary(asset: Optional[Asset], limit: int = 10) -> List[dict]:
    if not asset:
        return []
    tickets = (
        Ticket.query.filter_by(asset_id=asset.id)
        .order_by(Ticket.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            'ticket_id': t.ticket_id,
            'title': t.title,
            'priority': t.priority,
            'status': t.status,
            'fault_type': t.fault_type,
            'created_at': t.created_at.isoformat() if t.created_at else None,
        }
        for t in tickets
    ]


def _candidate_technicians(supervisor_user_id: Optional[int] = None) -> List[dict]:
    """Return assignable User technicians (Ticket.technician_id stores User.id)."""
    candidates: List[User] = []
    if supervisor_user_id:
        team_ids = [
            row.technician_id
            for row in TicketSupervisorTeam.query.filter_by(supervisor_id=supervisor_user_id).all()
        ]
        if team_ids:
            candidates = User.query.filter(
                User.id.in_(team_ids),
                User.is_active.is_(True),
            ).all()
    if not candidates:
        # Fallback: active ticketing users who are not admins (broad roster)
        candidates = (
            User.query.filter(
                User.is_active.is_(True),
                User.access_ticketing.is_(True),
            )
            .limit(40)
            .all()
        )

    roster = Technician.query.filter(Technician.status == 'active').all()
    skill_by_email = {
        (r.email or '').strip().lower(): r.specialization
        for r in roster if r.email
    }
    skill_by_name = {
        (r.full_name or '').strip().lower(): r.specialization
        for r in roster if r.full_name
    }

    out = []
    for u in candidates:
        email = (u.email or '').strip().lower()
        name = (u.full_name or '').strip().lower()
        specialization = skill_by_email.get(email) or skill_by_name.get(name)
        out.append({
            'technician_id': u.id,
            'full_name': u.full_name,
            'email': u.email,
            'designation': u.designation,
            'specialization': specialization,
            'status': 'active' if u.is_active else 'inactive',
        })
    return out


def apply_rules(suggested: dict, allowed_technician_ids: set) -> dict:
    """Clamp / sanitize Claude output before UI or DB use."""
    priority = (suggested.get('priority') or 'medium').strip().lower()
    if priority not in ALLOWED_PRIORITIES:
        priority = 'medium'

    try:
        sla = int(suggested.get('sla_hours'))
    except (TypeError, ValueError):
        sla = 24
    sla = max(SLA_MIN, min(SLA_MAX, sla))

    tech_id = suggested.get('technician_id')
    if tech_id is not None:
        try:
            tech_id = int(tech_id)
        except (TypeError, ValueError):
            tech_id = None
        if tech_id not in allowed_technician_ids:
            tech_id = None

    parts = suggested.get('required_parts') or []
    if not isinstance(parts, list):
        parts = []
    parts = [str(p).strip() for p in parts if str(p).strip()][:20]

    reasoning = (suggested.get('reasoning') or '').strip()[:500]

    return {
        'priority': priority,
        'sla_hours': sla,
        'technician_id': tech_id,
        'required_parts': parts,
        'reasoning': reasoning,
    }


def triage_ticket(
    ticket_payload: dict,
    *,
    actor_user_id: Optional[int] = None,
    supervisor_user_id: Optional[int] = None,
    ticket_db_id: Optional[int] = None,
    ticket_code: Optional[str] = None,
    log_decision: str = 'preview',
) -> Dict[str, Any]:
    """
    Run Claude triage + rules. Returns:
      { success, suggestion, triage_log_id, raw_response?, error? }
    Does not mutate the Ticket row.
    """
    if not is_llm_enabled():
        return {'success': False, 'error': 'LLM is not enabled', 'suggestion': None}

    asset = None
    asset_pk = ticket_payload.get('asset_id') or ticket_payload.get('asset_pk')
    asset_code = (ticket_payload.get('asset_code') or '').strip()
    if asset_pk:
        try:
            asset = Asset.query.get(int(asset_pk))
        except (TypeError, ValueError):
            asset = None
    if asset is None and asset_code:
        asset = Asset.query.filter_by(asset_id=asset_code).first()

    candidates = _candidate_technicians(supervisor_user_id)
    allowed_ids = {c['technician_id'] for c in candidates}

    inputs = {
        'title': ticket_payload.get('title'),
        'work_description': ticket_payload.get('work_description'),
        'project': ticket_payload.get('project'),
        'service_group': ticket_payload.get('service_group'),
        'category': ticket_payload.get('category'),
        'fault_type': ticket_payload.get('fault_type'),
        'property_name': ticket_payload.get('property_name'),
        'zone': ticket_payload.get('zone'),
        'sub_zone': ticket_payload.get('sub_zone'),
        'base_unit': ticket_payload.get('base_unit'),
        'asset': asset.to_dict() if asset else None,
        'asset_history': _asset_history_summary(asset),
        'technicians': candidates,
    }

    user_content = (
        'Triage this work order. Return JSON only.\n\n'
        + json.dumps(inputs, default=str)
    )

    raw_text = None
    try:
        raw = generate_structured(SYSTEM_PROMPT, user_content, TRIAGE_SCHEMA)
        # Keep a copy for audit — generate_structured already parsed
        raw_text = json.dumps(raw)
        suggestion = apply_rules(raw, allowed_ids)
    except StructuredLLMError as exc:
        logger.warning('Triage LLM error: %s', exc)
        log = TicketTriageLog(
            ticket_id=ticket_db_id,
            ticket_code=ticket_code,
            actor_user_id=actor_user_id,
            prompt_inputs=inputs,
            raw_response=str(exc),
            suggested=None,
            decision='error',
        )
        db.session.add(log)
        db.session.commit()
        return {
            'success': False,
            'error': str(exc),
            'suggestion': None,
            'triage_log_id': log.id,
        }

    # Enrich with technician name for UI
    tech_name = None
    if suggestion['technician_id']:
        for c in candidates:
            if c['technician_id'] == suggestion['technician_id']:
                tech_name = c['full_name']
                break
    suggestion['technician_name'] = tech_name

    log = TicketTriageLog(
        ticket_id=ticket_db_id,
        ticket_code=ticket_code,
        actor_user_id=actor_user_id,
        prompt_inputs=inputs,
        raw_response=raw_text,
        suggested=suggestion,
        decision=log_decision,
    )
    db.session.add(log)
    db.session.commit()

    return {
        'success': True,
        'suggestion': suggestion,
        'triage_log_id': log.id,
        'candidates': candidates,
    }


CLASSIFY_SCHEMA = {
    'required': ['fault_code_id', 'reasoning'],
    'properties': {
        'fault_code_id': (int, type(None)),
        'reasoning': str,
    },
}

CLASSIFY_SYSTEM_PROMPT = (
    'You are an FM work-order classification assistant for Kynvera. '
    'You are given the full fault-code catalog, one entry per line as '
    '"id<TAB>service_group<TAB>category<TAB>fault name", and a work-order title/description '
    '(often copied straight from an inbound email, so it may be terse or informal). '
    'Return the single fault_code_id whose fault name is the best match for the complaint. '
    'Only return an id that literally appears in the catalog — never invent one. '
    'If nothing in the catalog is a reasonable match, return fault_code_id: null. '
    'reasoning is one short sentence explaining the match, or why nothing fit.'
)


def _fault_catalog_rows() -> List[dict]:
    from module_ticketing import ticket_field_catalog as tkt_fields
    opts = tkt_fields.classification_options()
    return (opts or {}).get('fault_catalog') or []


def classify_ticket(ticket_payload: dict) -> Dict[str, Any]:
    """Suggest Service Group / Category / Fault code from title + description.

    Unlike triage_ticket, this never writes an audit log row — it's a stateless
    lookup against the existing fault catalog, re-run freely from the UI.
    Returns: { success, suggestion: {..} | None, reasoning?, error? }
    Never mutates the Ticket row.
    """
    if not is_llm_enabled():
        return {'success': False, 'error': 'LLM is not enabled', 'suggestion': None}

    title = (ticket_payload.get('title') or '').strip()
    description = (ticket_payload.get('work_description') or '').strip()
    if not title and not description:
        return {'success': False, 'error': 'title or work_description required', 'suggestion': None}

    rows = _fault_catalog_rows()
    if not rows:
        return {'success': False, 'error': 'No fault catalog configured', 'suggestion': None}

    by_id = {r['id']: r for r in rows if r.get('id') is not None}
    catalog_block = '\n'.join(
        f"{r['id']}\t{r.get('service_group') or ''}\t{r.get('fault_category') or ''}\t{r.get('fault_code_name') or ''}"
        for r in rows if r.get('id') is not None
    )

    user_content = (
        'Classify this work order against the fault catalog below. Return JSON only.\n\n'
        f'Title: {title}\n'
        f'Description: {description}\n\n'
        'Fault catalog (id<TAB>service_group<TAB>category<TAB>fault name):\n'
        + catalog_block
    )

    try:
        raw = generate_structured(CLASSIFY_SYSTEM_PROMPT, user_content, CLASSIFY_SCHEMA)
    except StructuredLLMError as exc:
        logger.warning('Classification LLM error: %s', exc)
        return {'success': False, 'error': str(exc), 'suggestion': None}

    reasoning = (raw.get('reasoning') or '').strip()[:500]
    match = by_id.get(raw.get('fault_code_id'))
    if not match:
        return {'success': True, 'suggestion': None, 'reasoning': reasoning}

    return {
        'success': True,
        'suggestion': {
            'fault_code_id': match['id'],
            'service_group': match.get('service_group'),
            'category': match.get('fault_category'),
            'fault_code': match.get('fault_code'),
            'fault_code_name': match.get('fault_code_name'),
            'fault_pick_value': match.get('fault_pick_value'),
            'reasoning': reasoning,
        },
    }


def confirm_triage(
    triage_log_id: int,
    accepted: dict,
    *,
    actor_user_id: Optional[int] = None,
    ticket: Optional[Ticket] = None,
    apply_to_ticket: bool = False,
) -> Dict[str, Any]:
    """Record human accept/override. Optionally apply priority/sla (not technician) to ticket."""
    log = TicketTriageLog.query.get(triage_log_id)
    if not log:
        return {'success': False, 'error': 'Triage log not found'}

    suggested = log.suggested or {}
    allowed_ids = set()
    if ticket and ticket.supervisor_id:
        allowed_ids = {c['technician_id'] for c in _candidate_technicians(ticket.supervisor_id)}
    else:
        allowed_ids = {c['technician_id'] for c in _candidate_technicians()}

    cleaned = apply_rules({**suggested, **(accepted or {})}, allowed_ids)
    decision = 'accepted'
    if suggested and (
        cleaned.get('priority') != suggested.get('priority')
        or cleaned.get('sla_hours') != suggested.get('sla_hours')
        or cleaned.get('technician_id') != suggested.get('technician_id')
    ):
        decision = 'overridden'

    log.accepted = cleaned
    log.decision = decision
    if actor_user_id:
        log.actor_user_id = actor_user_id

    if apply_to_ticket and ticket:
        ticket.priority = cleaned['priority']
        ticket.sla_hours = cleaned['sla_hours']
        # Do not auto-assign technician — supervisor confirms via assign API

    db.session.commit()
    return {'success': True, 'accepted': cleaned, 'decision': decision, 'triage_log_id': log.id}
