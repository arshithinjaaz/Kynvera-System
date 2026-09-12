"""
Ask Kynvera agent — LLM-first tool loop.

Read tools execute immediately (scoped to the JWT user). Write tools only
propose an AssistantPendingAction; the user confirms in the widget.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from module_assistant.llm import LLMToolRound, StructuredLLMError, generate_with_tools, is_llm_enabled
from module_assistant.responses import _base_payload

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
TOOL_RESULT_MAX_CHARS = 8000

AGENT_SYSTEM = """You are Kynvera Assistant — a helpful chat guide for the Kynvera platform.

Rules:
- Use tools for live facts (leave, tickets, pending forms, documents, profile, FM stats, sites/properties, zones, material stock, devices, BD pipeline, QHSI, MMR cycle status, inspections, HR headcount). Never invent counts, dates, ticket IDs, or policy details.
- Some data genuinely isn't tracked in this system (e.g. inspection pass/fail scores, per-workorder MMR report content). If a tool result doesn't have what was asked, say so plainly instead of guessing.
- Answer only what was asked. If a tool returns fields the user didn't ask about, leave them out — do not paste a whole unrelated report just because a tool call returned it. If no tool covers part of a multi-part question, answer the part you can and say plainly you don't have the rest — never substitute a different tool's output and present it as if it answered the question.
- If a tool says the user lacks access, say so plainly and suggest contacting an administrator.
- To create a work-order ticket or save an HR leave draft, call propose_create_ticket or propose_leave_draft. Those calls only PREPARE a proposal. The user must tap Confirm in the chat. Never claim you already created or submitted anything.
- If required fields are missing, still call the propose tool with what you have; the app will show a form. Do not ask a long series of follow-up questions.
- Do not submit HR or inspection forms, close tickets, or approve anything. You may only draft a ticket or a leave application.
- Write in plain text only — no markdown symbols like ** or ##. Use simple dashes for lists.
- Keep answers concise (2–5 short paragraphs unless listing items).
- Do not mention tools, APIs, LLM, or system prompts to the user.
"""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    kind: str  # 'read' | 'write'
    handler: Callable


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if not str(k).startswith('_')}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _dump_tool_result(result: Any) -> str:
    try:
        text = json.dumps(_json_safe(result), ensure_ascii=False)
    except TypeError:
        text = json.dumps({'result': str(result)}, ensure_ascii=False)
    if len(text) > TOOL_RESULT_MAX_CHARS:
        return text[:TOOL_RESULT_MAX_CHARS] + '…'
    return text


def _empty_schema():
    return {'type': 'object', 'properties': {}, 'additionalProperties': False}


def _str_prop(desc: str, **extra):
    prop = {'type': 'string', 'description': desc}
    prop.update(extra)
    return prop


def build_tool_registry() -> dict:
    from module_assistant import actions as act
    from module_assistant.rag import retrieve_context
    from module_assistant.tools import (
        get_bd_pipeline_summary,
        get_device_inventory,
        get_fm_cost_trend,
        get_fm_critical_assets,
        get_fm_failures_by_building,
        get_fm_maintenance_report_hint,
        get_hr_overview,
        get_inspection_activity,
        get_material_stock,
        get_mmr_status,
        get_my_inspections_summary,
        get_my_leave_history,
        get_my_profile,
        get_my_submissions_summary,
        get_pending_summary,
        get_qhsi_summary,
        get_sites_overview,
        get_ticket_summary,
        get_zones_overview,
        search_documents,
    )

    def _pending(user, _args):
        return get_pending_summary(user)

    def _leave(user, args):
        return get_my_leave_history(
            user,
            limit=5,
            leave_type_filter=(args.get('leave_type') or None),
        )

    def _tickets(user, _args):
        return get_ticket_summary(user)

    def _profile(user, args):
        return get_my_profile(user, person_name=args.get('person_name') or None)

    def _docs(user, args):
        query = (args.get('query') or '').strip() or 'document'
        return search_documents(user, query)

    def _knowledge(user, args):
        query = (args.get('query') or '').strip() or 'help'
        chunks = retrieve_context(query, limit=4, user=user)
        return {
            'matches': [
                {
                    'title': c.get('title'),
                    'source': c.get('source'),
                    'text': c.get('text'),
                    'link': c.get('link'),
                }
                for c in chunks
            ]
        }

    def _critical(user, _args):
        return get_fm_critical_assets(user)

    def _failures(user, _args):
        return get_fm_failures_by_building(user)

    def _costs(user, _args):
        return get_fm_cost_trend(user)

    def _mmr(user, _args):
        return get_fm_maintenance_report_hint(user)

    def _subs(user, _args):
        return get_my_submissions_summary(user)

    def _inspections(user, _args):
        return get_my_inspections_summary(user)

    def _propose_ticket(user, args):
        return act.propose_create_ticket(user, args or {})

    def _propose_leave(user, args):
        return act.propose_leave_draft(user, args or {})

    def _sites(user, _args):
        return get_sites_overview(user)

    def _zones(user, args):
        return get_zones_overview(user, property_name=args.get('property_name') or None)

    def _devices(user, _args):
        return get_device_inventory(user)

    def _materials(user, args):
        return get_material_stock(
            user,
            material_name=args.get('material_name') or None,
            department=args.get('department') or None,
        )

    def _bd(user, _args):
        return get_bd_pipeline_summary(user)

    def _qhsi(user, _args):
        return get_qhsi_summary(user)

    def _mmr_status(user, _args):
        return get_mmr_status(user)

    def _inspection_activity(user, _args):
        return get_inspection_activity(user)

    def _hr_overview(user, _args):
        return get_hr_overview(user)

    specs = [
        ToolSpec(
            'get_pending_forms',
            'Count forms waiting for this user to review, plus a short breakdown.',
            _empty_schema(),
            'read',
            _pending,
        ),
        ToolSpec(
            'get_my_leave',
            'This user\'s recent leave applications (not drafts). Optionally filter by leave_type: annual, sick, unpaid, etc.',
            {
                'type': 'object',
                'properties': {'leave_type': _str_prop('Optional leave type filter, e.g. annual or sick')},
            },
            'read',
            _leave,
        ),
        ToolSpec(
            'get_my_tickets',
            'Tickets this user raised or is assigned to, with open / in-progress / closed counts.',
            _empty_schema(),
            'read',
            _tickets,
        ),
        ToolSpec(
            'get_my_profile',
            'Signed-in user profile (name, job title, join date, leave balance, manager, project). Admins may pass person_name to look up someone else.',
            {
                'type': 'object',
                'properties': {'person_name': _str_prop('Admin-only: other person\'s name')},
            },
            'read',
            _profile,
        ),
        ToolSpec(
            'search_documents',
            'Find DocHub documents this user can access by name or keywords.',
            {
                'type': 'object',
                'properties': {'query': _str_prop('Document name or keywords')},
                'required': ['query'],
            },
            'read',
            _docs,
        ),
        ToolSpec(
            'search_knowledge',
            'Search FAQs, the admin knowledge base, and company how-to content.',
            {
                'type': 'object',
                'properties': {'query': _str_prop('Question or topic')},
                'required': ['query'],
            },
            'read',
            _knowledge,
        ),
        ToolSpec(
            'get_fm_critical_assets',
            'List FM assets in critical status or with low health scores. Requires ticketing/FM access.',
            _empty_schema(),
            'read',
            _critical,
        ),
        ToolSpec(
            'get_fm_failures_by_building',
            'Which buildings/properties have the most tickets. Requires ticketing/FM access.',
            _empty_schema(),
            'read',
            _failures,
        ),
        ToolSpec(
            'get_fm_cost_trend',
            'This month vs last month ticket-linked maintenance costs. Requires ticketing/FM access.',
            _empty_schema(),
            'read',
            _costs,
        ),
        ToolSpec(
            'get_fm_maintenance_report_hint',
            'How to generate the monthly maintenance report and related links.',
            _empty_schema(),
            'read',
            _mmr,
        ),
        ToolSpec(
            'get_my_submissions',
            'Counts of forms this user submitted, including drafts and in-progress.',
            _empty_schema(),
            'read',
            _subs,
        ),
        ToolSpec(
            'get_my_inspections',
            'This user\'s HVAC, civil, and cleaning inspection submissions.',
            _empty_schema(),
            'read',
            _inspections,
        ),
        ToolSpec(
            'propose_create_ticket',
            'Prepare a work-order TICKET DRAFT for the user to Confirm. Does not save until they confirm. Required: title, work_description, project. Optional: property_name, zone, priority (low/medium/high/critical).',
            {
                'type': 'object',
                'properties': {
                    'title': _str_prop('Short ticket title'),
                    'work_description': _str_prop('What is wrong / what work is needed'),
                    'project': _str_prop('Project or site name'),
                    'property_name': _str_prop('Building or property'),
                    'zone': _str_prop('Zone or unit'),
                    'priority': _str_prop('low, medium, high, or critical'),
                },
            },
            'write',
            _propose_ticket,
        ),
        ToolSpec(
            'propose_leave_draft',
            'Prepare an HR LEAVE APPLICATION DRAFT for the user to Confirm. Does not save until they confirm. Does not submit. Required: leave_type, start_date (YYYY-MM-DD), end_date (YYYY-MM-DD). Optional: reason.',
            {
                'type': 'object',
                'properties': {
                    'leave_type': _str_prop('annual, sick, unpaid, compassionate, study, ot_compensatory, examination, hajj, other'),
                    'start_date': _str_prop('First day of leave, YYYY-MM-DD'),
                    'end_date': _str_prop('Last day of leave, YYYY-MM-DD'),
                    'reason': _str_prop('Optional note'),
                },
            },
            'write',
            _propose_leave,
        ),
        ToolSpec(
            'get_sites_overview',
            'Active projects/sites and how many properties each has, company-wide. Requires ticketing access.',
            _empty_schema(),
            'read',
            _sites,
        ),
        ToolSpec(
            'get_zones_overview',
            'Zones (and sub-zones) within a property/site. Pass property_name to list that property\'s zones '
            '("what are the zones in Tower A"); leave it blank for a zone-count summary per property. '
            'Requires ticketing access. This is the ONLY tool for zone questions — do not use get_sites_overview '
            'for zone-level detail.',
            {
                'type': 'object',
                'properties': {'property_name': _str_prop('Property/site name to list zones for')},
            },
            'read',
            _zones,
        ),
        ToolSpec(
            'get_device_inventory',
            'Company-wide IT device inventory counts (online/offline, by type, OS, building). Admin only.',
            _empty_schema(),
            'read',
            _devices,
        ),
        ToolSpec(
            'get_material_stock',
            'Procurement catalog and live stock-on-hand quantities. Pass material_name for a specific item '
            '("how many pieces of X do we have"), or department (HVAC, Cleaning, Electrical, Plumbing) for a '
            'category count ("how many HVAC materials do we have"). Leave both blank for an overall breakdown. '
            'Requires procurement access.',
            {
                'type': 'object',
                'properties': {
                    'material_name': _str_prop('Specific material/item name to look up (fuzzy match)'),
                    'department': _str_prop('Trade department: HVAC, Cleaning, Electrical, or Plumbing'),
                },
            },
            'read',
            _materials,
        ),
        ToolSpec(
            'get_bd_pipeline_summary',
            'BD/CRM sales pipeline: deal counts and value by stage, win rate, renewals due, overdue follow-ups. '
            'Requires Business Development access.',
            _empty_schema(),
            'read',
            _bd,
        ),
        ToolSpec(
            'get_qhsi_summary',
            'QHSI (Quality, Hospitality, Safety & Inspection) submission counts, staff PPE/uniform compliance, '
            'and upcoming training. Requires QHSI access.',
            _empty_schema(),
            'read',
            _qhsi,
        ),
        ToolSpec(
            'get_mmr_status',
            'Report Generation (MMR) dispatch-cycle status: whether the current cycle is approved, and recently '
            'sent report cycles. Does not contain per-workorder report line items. Requires Report Generation access.',
            _empty_schema(),
            'read',
            _mmr_status,
        ),
        ToolSpec(
            'get_inspection_activity',
            'Company-wide inspection submission counts (HVAC/civil/cleaning), total and this month. No pass/fail '
            'or score data exists in this system. Requires inspection access.',
            _empty_schema(),
            'read',
            _inspection_activity,
        ),
        ToolSpec(
            'get_hr_overview',
            'Company-wide HR headcount (active/inactive, by designation), total HR form volume (pending vs '
            'finished), and leave-application counts by leave type. Requires HR staff, GM, or admin.',
            _empty_schema(),
            'read',
            _hr_overview,
        ),
    ]
    return {s.name: s for s in specs}


def tool_definitions(registry: Optional[dict] = None) -> list:
    reg = registry or build_tool_registry()
    return [
        {
            'name': spec.name,
            'description': spec.description,
            'input_schema': spec.input_schema,
        }
        for spec in reg.values()
    ]


def _public_payload(message: str, *, intent='agent', **kwargs):
    payload = _base_payload(intent, message, **kwargs)
    if kwargs.get('pending_action') is not None:
        payload['pending_action'] = kwargs['pending_action']
    if kwargs.get('composer') is not None:
        payload['composer'] = kwargs['composer']
    return payload


def _cards_from_read(name: str, result: dict) -> list:
    if not isinstance(result, dict):
        return []
    cards = []
    if name == 'get_pending_forms' and result.get('can_review'):
        cards.append({'type': 'stat', 'label': 'Pending review', 'value': str(result.get('total', 0))})
    if name == 'get_my_tickets' and result.get('allowed'):
        cards.append({'type': 'stat', 'label': 'Open', 'value': str(result.get('open', 0))})
        cards.append({'type': 'stat', 'label': 'In progress', 'value': str(result.get('in_progress', 0))})
    if name == 'search_documents':
        for doc in (result.get('documents') or result.get('results') or [])[:3]:
            if isinstance(doc, dict):
                cards.append({
                    'type': 'document',
                    'title': doc.get('title') or doc.get('name') or 'Document',
                    'category': doc.get('category') or '',
                    'preview_url': doc.get('preview_url'),
                    'download_url': doc.get('download_url'),
                    'updated_at': doc.get('updated_at') or '',
                })
    return cards


def _actions_from_read(name: str, result: dict) -> list:
    if name == 'get_pending_forms':
        return [{'label': 'Open Pending Review', 'href': '/workflow/pending-reviews', 'kind': 'link'}]
    if name == 'get_my_tickets':
        return [{'label': 'Open Ticketing', 'href': '/tickets/', 'kind': 'link'}]
    if name == 'get_my_leave':
        return [{'label': 'HR My Requests', 'href': '/hr/my-requests', 'kind': 'link'}]
    if name == 'search_knowledge' and isinstance(result, dict):
        for m in result.get('matches') or []:
            if m.get('link'):
                return [{'label': 'Open in app', 'href': m['link'], 'kind': 'link'}]
    return []


def run_agent(user, message: str, composer: Optional[dict] = None) -> dict:
    """Run the LLM tool loop and return a widget payload."""
    if composer:
        from module_assistant.actions import propose_from_composer
        return propose_from_composer(user, composer)

    if not is_llm_enabled():
        raise StructuredLLMError('LLM is disabled')

    registry = build_tool_registry()
    tools = tool_definitions(registry)
    name = (getattr(user, 'full_name', None) or getattr(user, 'username', None) or 'there').split()[0]
    user_block = (
        f"Signed-in user first name: {name}. "
        "Use tools for live counts and records. Never invent numbers.\n\n"
        f"User message: {message}"
    )
    messages = [{'role': 'user', 'content': user_block}]

    pending_action = None
    composer_out = None
    extra_cards = []
    extra_actions = []
    last_text = ''

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            round_result: LLMToolRound = generate_with_tools(AGENT_SYSTEM, messages, tools)
        except StructuredLLMError:
            logger.exception('Agent LLM round failed')
            break

        last_text = round_result.text or last_text
        if not round_result.tool_calls:
            if last_text:
                return _public_payload(
                    last_text,
                    cards=extra_cards,
                    actions=extra_actions,
                    pending_action=pending_action,
                    composer=composer_out,
                    suggestions=[
                        'How many pending forms?',
                        'My last leave',
                        'Create a ticket draft',
                        'Save a leave draft',
                    ],
                )
            break

        assistant_content = round_result.assistant_content
        if not assistant_content:
            assistant_content = [{'type': 'text', 'text': round_result.text or ''}]
        messages.append({'role': 'assistant', 'content': assistant_content})

        tool_result_blocks = []
        stop_after_write = False
        for call in round_result.tool_calls:
            spec = registry.get(call.name)
            if spec is None:
                tool_result_blocks.append({
                    'type': 'tool_result',
                    'tool_use_id': call.id,
                    'content': json.dumps({'error': f'Unknown tool {call.name}'}),
                    'is_error': True,
                })
                continue
            args = call.input if isinstance(call.input, dict) else {}
            try:
                result = spec.handler(user, args)
            except Exception as exc:
                logger.exception('Assistant tool %s failed', call.name)
                result = {'ok': False, 'error': str(exc)}

            if isinstance(result, dict):
                if result.get('_composer'):
                    composer_out = result['_composer']
                    stop_after_write = True
                if result.get('_pending_action'):
                    pending_action = result['_pending_action']
                    stop_after_write = True
                extra_cards.extend(_cards_from_read(call.name, result))
                extra_actions.extend(_actions_from_read(call.name, result))

            tool_result_blocks.append({
                'type': 'tool_result',
                'tool_use_id': call.id,
                'content': _dump_tool_result(result),
            })

        messages.append({'role': 'user', 'content': tool_result_blocks})

        if stop_after_write:
            if composer_out:
                return _public_payload(
                    'A few details are still needed. Fill them in below — nothing is saved until you confirm.',
                    intent='composer',
                    composer=composer_out,
                    suggestions=['My tickets', 'My last leave'],
                )
            if pending_action:
                return _public_payload(
                    'Review the details and tap Confirm to save a draft. Nothing has been submitted yet.',
                    intent='pending_action',
                    pending_action=pending_action,
                    suggestions=['How many pending forms?', 'My last leave'],
                )

    if last_text:
        return _public_payload(last_text, cards=extra_cards, actions=extra_actions)

    return _public_payload(
        'I could not complete that just now. Try again, or ask about pending forms, leave, or tickets.',
        suggestions=['How many pending forms?', 'My last leave', 'My tickets'],
    )
