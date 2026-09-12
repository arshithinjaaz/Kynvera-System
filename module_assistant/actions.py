"""
Confirm-before-write actions for the Ask Kynvera agent.

Write tools never mutate tickets/forms in the chat request. They store an
AssistantPendingAction; /api/assistant/confirm executes it.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models import AuditLog, AssistantPendingAction, Submission, Ticket, TicketNote, User, db

logger = logging.getLogger(__name__)

PENDING_TTL_MINUTES = 15
ACTION_CREATE_TICKET = 'create_ticket'
ACTION_LEAVE_DRAFT = 'leave_draft'

ALLOWED_PRIORITIES = frozenset({'low', 'medium', 'high', 'critical'})
LEAVE_TYPES = {
    'annual', 'sick', 'ot_compensatory', 'compassionate', 'study',
    'unpaid', 'examination', 'hajj', 'other',
}
LEAVE_TYPE_ALIASES = {
    'annual leave': 'annual',
    'vacation': 'annual',
    'holiday': 'annual',
    'sick leave': 'sick',
    'compensatory': 'ot_compensatory',
    'ot compensatory': 'ot_compensatory',
    'comp off': 'ot_compensatory',
    'compassionate leave': 'compassionate',
    'study leave': 'study',
    'unpaid leave': 'unpaid',
    'exam': 'examination',
    'examination leave': 'examination',
    'hajj leave': 'hajj',
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_ticketing(user) -> bool:
    return bool(
        getattr(user, 'role', None) == 'admin'
        or getattr(user, 'access_ticketing', False)
    )


def _normalize_priority(value) -> str:
    p = (value or 'medium').strip().lower()
    return p if p in ALLOWED_PRIORITIES else 'medium'


def _normalize_leave_type(value) -> str:
    raw = (value or '').strip().lower().replace('-', '_')
    if raw in LEAVE_TYPES:
        return raw
    return LEAVE_TYPE_ALIASES.get(raw.replace('_', ' '), '') or raw


def ticket_composer(prefill: Optional[dict] = None) -> dict:
    p = prefill or {}
    return {
        'type': ACTION_CREATE_TICKET,
        'title': 'Create a ticket draft',
        'hint': 'Fill the required fields, then Confirm. This saves a draft for supervisor review — it is not submitted into the workflow yet.',
        'fields': [
            {'name': 'title', 'label': 'Title', 'type': 'text', 'required': True, 'value': p.get('title') or ''},
            {'name': 'work_description', 'label': 'Description', 'type': 'textarea', 'required': True, 'value': p.get('work_description') or ''},
            {'name': 'project', 'label': 'Project', 'type': 'text', 'required': True, 'value': p.get('project') or ''},
            {'name': 'property_name', 'label': 'Property / building', 'type': 'text', 'required': False, 'value': p.get('property_name') or ''},
            {'name': 'zone', 'label': 'Zone', 'type': 'text', 'required': False, 'value': p.get('zone') or ''},
            {
                'name': 'priority',
                'label': 'Priority',
                'type': 'select',
                'required': False,
                'value': _normalize_priority(p.get('priority')),
                'options': [
                    {'value': 'low', 'label': 'Low'},
                    {'value': 'medium', 'label': 'Medium'},
                    {'value': 'high', 'label': 'High'},
                    {'value': 'critical', 'label': 'Critical'},
                ],
            },
        ],
    }


def leave_composer(prefill: Optional[dict] = None) -> dict:
    p = prefill or {}
    return {
        'type': ACTION_LEAVE_DRAFT,
        'title': 'Save a leave draft',
        'hint': 'This saves a draft only. Open the leave form to review, sign, and submit.',
        'fields': [
            {
                'name': 'leave_type',
                'label': 'Leave type',
                'type': 'select',
                'required': True,
                'value': _normalize_leave_type(p.get('leave_type')) or 'annual',
                'options': [
                    {'value': 'annual', 'label': 'Annual Leave'},
                    {'value': 'sick', 'label': 'Sick Leave'},
                    {'value': 'ot_compensatory', 'label': 'OT Compensatory Off'},
                    {'value': 'compassionate', 'label': 'Compassionate Leave'},
                    {'value': 'study', 'label': 'Study Leave'},
                    {'value': 'unpaid', 'label': 'Unpaid Leave'},
                    {'value': 'examination', 'label': 'Examination Leave'},
                    {'value': 'hajj', 'label': 'Hajj Leave'},
                    {'value': 'other', 'label': 'Other'},
                ],
            },
            {'name': 'start_date', 'label': 'First day', 'type': 'date', 'required': True, 'value': p.get('start_date') or p.get('first_day_of_leave') or ''},
            {'name': 'end_date', 'label': 'Last day', 'type': 'date', 'required': True, 'value': p.get('end_date') or p.get('last_day_of_leave') or ''},
            {'name': 'reason', 'label': 'Reason (optional)', 'type': 'textarea', 'required': False, 'value': p.get('reason') or ''},
        ],
    }


def _missing_ticket_fields(fields: dict) -> list:
    missing = []
    if not (fields.get('title') or '').strip():
        missing.append('title')
    if not (fields.get('work_description') or '').strip():
        missing.append('work_description')
    if not (fields.get('project') or '').strip():
        missing.append('project')
    return missing


def _missing_leave_fields(fields: dict) -> list:
    missing = []
    lt = _normalize_leave_type(fields.get('leave_type'))
    if not lt:
        missing.append('leave_type')
    start = (fields.get('start_date') or fields.get('first_day_of_leave') or '').strip()
    end = (fields.get('end_date') or fields.get('last_day_of_leave') or '').strip()
    if not start:
        missing.append('start_date')
    if not end:
        missing.append('end_date')
    return missing


def store_pending_action(user, action_type: str, fields: dict, summary: dict) -> AssistantPendingAction:
    row = AssistantPendingAction(
        user_id=user.id,
        action_type=action_type,
        payload={'fields': fields, 'summary': summary},
        status='pending',
        expires_at=_utcnow() + timedelta(minutes=PENDING_TTL_MINUTES),
    )
    db.session.add(row)
    db.session.commit()
    return row


def propose_create_ticket(user, args: dict) -> dict:
    """Propose a ticket draft. Never writes a Ticket row."""
    if not _has_ticketing(user):
        return {'ok': False, 'error': 'You do not have Ticketing access. Ask an administrator to grant it.'}

    fields = {
        'title': (args.get('title') or '').strip(),
        'work_description': (args.get('work_description') or args.get('description') or '').strip(),
        'project': (args.get('project') or getattr(user, 'assigned_project', None) or '').strip(),
        'property_name': (args.get('property_name') or '').strip(),
        'zone': (args.get('zone') or '').strip(),
        'priority': _normalize_priority(args.get('priority')),
    }
    missing = _missing_ticket_fields(fields)
    if missing:
        return {
            'ok': False,
            'missing': missing,
            '_composer': ticket_composer(fields),
            'error': 'Missing required ticket fields. A form is shown in the chat for the user to complete.',
        }

    summary = {
        'title': fields['title'],
        'project': fields['project'],
        'priority': fields['priority'],
        'property_name': fields['property_name'] or '—',
    }
    row = store_pending_action(user, ACTION_CREATE_TICKET, fields, summary)
    return {
        'ok': True,
        'status': 'awaiting_user_confirm',
        'message': 'Ticket draft is ready. The user must tap Confirm — nothing has been saved yet.',
        '_pending_action': row.to_public_dict(),
    }


def propose_leave_draft(user, args: dict) -> dict:
    """Propose an HR leave draft. Never writes a Submission row."""
    fields = {
        'leave_type': _normalize_leave_type(args.get('leave_type')),
        'start_date': (args.get('start_date') or args.get('first_day_of_leave') or '').strip()[:10],
        'end_date': (args.get('end_date') or args.get('last_day_of_leave') or '').strip()[:10],
        'reason': (args.get('reason') or '').strip(),
    }
    missing = _missing_leave_fields(fields)
    if missing:
        return {
            'ok': False,
            'missing': missing,
            '_composer': leave_composer(fields),
            'error': 'Missing required leave fields. A form is shown in the chat for the user to complete.',
        }

    summary = {
        'leave_type': fields['leave_type'],
        'start_date': fields['start_date'],
        'end_date': fields['end_date'],
    }
    row = store_pending_action(user, ACTION_LEAVE_DRAFT, fields, summary)
    return {
        'ok': True,
        'status': 'awaiting_user_confirm',
        'message': 'Leave draft is ready. The user must tap Confirm — nothing has been saved yet.',
        '_pending_action': row.to_public_dict(),
    }


def get_owned_pending(action_id, user) -> AssistantPendingAction:
    try:
        aid = int(action_id)
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid action id') from exc
    row = db.session.get(AssistantPendingAction, aid)
    if not row:
        raise LookupError('Action not found')
    if row.user_id != user.id:
        raise PermissionError('Not your action')
    return row


def _mark_expired_if_needed(row: AssistantPendingAction) -> bool:
    if row.status == 'pending' and row.expires_at and row.expires_at < _utcnow():
        row.status = 'expired'
        db.session.commit()
        return True
    return False


def cancel_pending(row: AssistantPendingAction) -> dict:
    if _mark_expired_if_needed(row):
        return {'ok': False, 'error': 'This proposal has expired. Ask again to create a new one.'}
    if row.status != 'pending':
        return {'ok': False, 'error': f'This proposal is already {row.status}.'}
    row.status = 'cancelled'
    db.session.commit()
    return {'ok': True, 'status': 'cancelled', 'message': 'Cancelled. Nothing was saved.'}


def _log_audit(user, action, resource_type, resource_id, details):
    try:
        db.session.add(AuditLog(
            user_id=user.id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=details,
        ))
    except Exception:
        logger.exception('Failed to queue assistant audit log')


def _execute_create_ticket(user, fields: dict) -> dict:
    if not _has_ticketing(user):
        return {'ok': False, 'error': 'You do not have Ticketing access.'}

    project = (fields.get('project') or '').strip() or 'Unassigned'
    supervisor_id = None
    try:
        from module_ticketing.routes import _resolve_project_supervisor_id
        supervisor_id = _resolve_project_supervisor_id(project)
    except Exception:
        logger.debug('Could not resolve project supervisor', exc_info=True)

    ticket = Ticket(
        ticket_id='TKT-' + uuid.uuid4().hex[:8].upper(),
        reporter_id=user.id,
        assigned_to_id=supervisor_id,
        supervisor_id=supervisor_id,
        title=(fields.get('title') or '').strip()[:255],
        project=project[:160],
        service_group='Unclassified',
        category='Unclassified',
        fault_type='Unclassified',
        priority=_normalize_priority(fields.get('priority')),
        work_description=(fields.get('work_description') or '').strip() or '(No description provided)',
        property_name=(fields.get('property_name') or '').strip() or None,
        zone=(fields.get('zone') or '').strip() or None,
        status='draft',
        source='assistant',
    )
    db.session.add(ticket)
    db.session.flush()
    db.session.add(TicketNote(
        ticket_id=ticket.id,
        user_id=user.id,
        content=f'Ticket {ticket.ticket_id} drafted via Ask Kynvera by {user.full_name}.',
        note_type='status_change',
    ))
    href = f'/tickets/{ticket.ticket_id}'
    _log_audit(user, 'assistant_create_ticket_draft', 'ticket', ticket.ticket_id, {
        'title': ticket.title,
        'project': ticket.project,
    })
    db.session.commit()
    return {
        'ok': True,
        'message': (
            f'Draft ticket {ticket.ticket_id} is saved. A supervisor still needs to review it '
            'before it enters the work-order workflow.'
        ),
        'ticket_id': ticket.ticket_id,
        'actions': [
            {'label': f'Open {ticket.ticket_id}', 'href': href, 'kind': 'link'},
            {'label': 'Ticket drafts', 'href': '/tickets/drafts/assistant', 'kind': 'link'},
        ],
    }


def _execute_leave_draft(user, fields: dict) -> dict:
    from module_assistant.tools import LEAVE_TYPE_LABELS, _profile_data_for

    profile = _profile_data_for(user)
    leave_type = _normalize_leave_type(fields.get('leave_type')) or 'annual'
    start = (fields.get('start_date') or '').strip()[:10]
    end = (fields.get('end_date') or '').strip()[:10]
    reason = (fields.get('reason') or '').strip()
    label = LEAVE_TYPE_LABELS.get(leave_type, leave_type.replace('_', ' ').title())

    total_days = ''
    try:
        from datetime import date as date_cls
        d0 = date_cls.fromisoformat(start)
        d1 = date_cls.fromisoformat(end)
        total_days = str((d1 - d0).days + 1)
    except Exception:
        pass

    form_data = {
        'employee_name': profile.get('full_name') or '',
        'job_title': profile.get('job_designation') or '',
        'employee_id': '',
        'department': (profile.get('designation') or '').replace('_', ' ').title(),
        'mobile_no': profile.get('phone') or '',
        'leave_type': leave_type,
        'leave_type_display': label,
        'first_day_of_leave': start,
        'last_day_of_leave': end,
        'total_days_requested': total_days,
        'reason': reason,
        'source': 'assistant',
    }
    if leave_type == 'other' and reason:
        form_data['leave_type_other'] = reason

    submission_id = f"draft_{uuid.uuid4().hex[:12]}"
    row = Submission(
        submission_id=submission_id,
        user_id=user.id,
        supervisor_id=user.id,
        module_type='hr_leave_application',
        site_name=f'{label} draft',
        visit_date=None,
        status='draft',
        workflow_status='draft',
        form_data=form_data,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.session.add(row)
    href = f'/hr/leave-application-form?edit={submission_id}'
    _log_audit(user, 'assistant_leave_draft', 'submission', submission_id, {
        'leave_type': leave_type,
        'start_date': start,
        'end_date': end,
    })
    db.session.commit()
    return {
        'ok': True,
        'message': (
            f'Leave draft saved ({label}, {start} to {end}). '
            'Open the form to review, add signatures, and submit — it has not been submitted.'
        ),
        'submission_id': submission_id,
        'actions': [
            {'label': 'Open leave form', 'href': href, 'kind': 'link'},
            {'label': 'HR My Requests', 'href': '/hr/my-requests', 'kind': 'link'},
        ],
    }


def execute_pending(row: AssistantPendingAction, user: User) -> dict:
    if _mark_expired_if_needed(row):
        return {'ok': False, 'error': 'This proposal has expired. Ask again to create a new one.'}
    if row.status != 'pending':
        return {'ok': False, 'error': f'This proposal is already {row.status}.'}
    if row.user_id != user.id:
        raise PermissionError('Not your action')

    payload = row.payload or {}
    fields = payload.get('fields') or payload
    try:
        if row.action_type == ACTION_CREATE_TICKET:
            result = _execute_create_ticket(user, fields)
        elif row.action_type == ACTION_LEAVE_DRAFT:
            result = _execute_leave_draft(user, fields)
        else:
            return {'ok': False, 'error': 'Unknown action type.'}
    except Exception:
        db.session.rollback()
        logger.exception('Assistant pending action failed')
        return {'ok': False, 'error': 'Could not complete that action. Please try again.'}

    if result.get('ok'):
        row.status = 'confirmed'
        db.session.commit()
    return result


def propose_from_composer(user, composer: dict) -> dict:
    """Turn a filled composer card into a pending action (or another composer)."""
    ctype = (composer.get('type') or '').strip()
    if ctype == ACTION_CREATE_TICKET:
        result = propose_create_ticket(user, composer)
    elif ctype == ACTION_LEAVE_DRAFT:
        result = propose_leave_draft(user, composer)
    else:
        return {
            'intent': 'agent',
            'message': 'I could not tell which form that was. Try asking to create a ticket or save a leave draft.',
            'cards': [],
            'actions': [],
            'sources': [],
            'suggestions': ['Create a ticket draft', 'Save a leave draft'],
        }

    from module_assistant.responses import _base_payload

    if result.get('_composer'):
        return _base_payload(
            'composer',
            'A few details are still needed. Fill them in below — nothing is saved until you confirm.',
            composer=result['_composer'],
            suggestions=['Cancel', 'My tickets', 'My last leave'],
        )
    if result.get('_pending_action'):
        pending = result['_pending_action']
        msg = (
            'Review the details and tap Confirm to save a draft. '
            'Nothing has been submitted yet.'
        )
        return _base_payload(
            'pending_action',
            msg,
            pending_action=pending,
            suggestions=['How many pending forms?', 'My last leave'],
        )
    return _base_payload(
        'agent',
        result.get('error') or result.get('message') or 'Could not prepare that action.',
        suggestions=['Create a ticket draft', 'Save a leave draft'],
    )
