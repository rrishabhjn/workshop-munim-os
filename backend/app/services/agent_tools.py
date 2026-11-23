"""Read & Pre-Flight Validation Tools for the Gemini 4 Hydrogen Interactions Loop.

All functions in this module are strictly SIDE-EFFECT FREE (read-only queries and
pre-flight checks against Supabase + Zoho Books India) so Gemini can ground homonyms,
active orders, proxy aliases, and payment accounts deterministically before proposing
or executing any state transition.
"""

from __future__ import annotations

import json
from typing import Any

from app.services import step_executors, zoho
from app.services.action_tools import ACTION_TOOLS_SCHEMA
from app.services.inventory_ai import INVENTORY_READ_TOOLS_SCHEMA, execute_inventory_read_tool


_READ_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_customers",
        "description": (
            "Search existing customers (active + past completed orders) and Zoho Contacts "
            "by name, surname, locality/area, or phone. ALWAYS call this before `stage_new_order` "
            "(to catch past/completed homonyms), directory edits, or Q&A. Do NOT call this before "
            "`stage_financial_entry` — financial tools already match customers and orders automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name, surname, or nickname (e.g. 'Sharma', 'Ambure')"},
                "area_or_phone": {"type": "string", "description": "Optional locality/city or phone fragment"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "search_projects_and_items",
        "description": (
            "Search customer orders (Lead -> Execution -> Completed) and their flat furniture items "
            "('Kitchen', 'Master Bed', etc.) by customer name, project code, or stage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Customer name, project code, or item name"},
                "project_id": {"type": "string", "description": "Exact UUID if already known from UI context"},
                "include_completed": {"type": "boolean", "description": "True to include closed/lost projects"},
            },
        },
    },
    {
        "type": "function",
        "name": "search_people_and_vendors",
        "description": (
            "Search office operators (Shreyansh, Ansh, Estimators), workshop/site Mistris, "
            "worker proxy payout aliases (e.g. 'Ruksar Bano' -> Lala Ji), and material/contractor Vendors. "
            "Use for directory edits or Q&A — do NOT call this before `stage_financial_entry` "
            "(it already matches payees and Zoho vendors automatically)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Person, mistri, proxy bank payee, or vendor name"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "query_workshop_pulse",
        "description": (
            "Answer live operational or financial questions: who's doing what / stuck items, "
            "Ansh's pending material orders, account/float balances, project P&L / customer dues, "
            "or recent ledger transactions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["whos_doing_what", "account_balances", "project_pnl", "needs_info", "recent_transactions"],
                },
                "filter_query": {"type": "string", "description": "Optional person, customer, or account filter"},
            },
            "required": ["topic"],
        },
    },
    {
        "type": "function",
        "name": "list_recent_transactions",
        "description": (
            "List recent money inflows, expenses, salary advances, vendor payments, float transfers, "
            "and supplier bills directly from live Zoho Books (or ledger), with optional worker/party, "
            "account (e.g. 'Cash Galla', 'Axis'), entry_type, or limit filter."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "party_or_search": {"type": "string", "description": "Optional worker, vendor, customer, proxy alias, or keyword"},
                "account": {"type": "string", "description": "Optional account name or code (e.g. 'Cash Galla', 'axis', 'rgb')"},
                "entry_type": {
                    "type": "string",
                    "enum": ["all", "customer_receipt", "expense", "vendor_payment", "float_transfer", "bill", "invoice"],
                },
                "limit": {"type": "integer", "description": "Number of transactions to return (default 15, max 50)"},
            },
        },
    },
]

INTERACTION_TOOLS_SCHEMA: list[dict[str, Any]] = [*_READ_TOOLS_SCHEMA, *INVENTORY_READ_TOOLS_SCHEMA, *ACTION_TOOLS_SCHEMA]
ALL_INTERACTION_TOOLS_SCHEMA = INTERACTION_TOOLS_SCHEMA


async def execute_read_tool(sb: Any, name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a side-effect-free tool call from Gemini and return compact JSON."""
    if name == "search_inventory":
        res = execute_inventory_read_tool(sb, arguments)
    elif name == "search_customers":
        res = await _tool_search_customers(
            sb,
            query=str(arguments.get("query") or ""),
            area_or_phone=str(arguments.get("area_or_phone") or ""),
        )
    elif name == "search_projects_and_items":
        res = _tool_search_projects_and_items(
            sb,
            query=str(arguments.get("query") or ""),
            project_id=arguments.get("project_id"),
            include_completed=bool(arguments.get("include_completed", False)),
        )
    elif name == "search_people_and_vendors":
        res = await _tool_search_people_and_vendors(
            sb, query=str(arguments.get("query") or "")
        )
    elif name == "list_recent_transactions":
        txns = await zoho.list_recent_ledger_transactions(
            sb,
            limit=max(1, min(int(arguments.get("limit") or 15), 50)),
            account_query=str(arguments.get("account") or ""),
            party_query=str(arguments.get("party_or_search") or ""),
            entry_type=str(arguments.get("entry_type") or ""),
        )
        res = {"transactions": txns}
    elif name == "query_workshop_pulse":
        res = await _tool_query_workshop_pulse(
            sb,
            topic=str(arguments.get("topic") or "whos_doing_what"),
            filter_query=str(arguments.get("filter_query") or ""),
        )
    else:
        res = {"error": f"Unknown tool: {name}"}

    return json.dumps(res, ensure_ascii=False, default=str)


async def _tool_search_customers(
    sb: Any, query: str, area_or_phone: str = ""
) -> dict[str, Any]:
    q = query.strip()
    if not q:
        return {"customers": [], "projects": [], "zoho_contacts": []}

    cust_rows = (
        sb.table("customers")
        .select("id,full_name,area_landmark,phone,zoho_contact_id,aliases")
        .ilike("full_name", f"%{q}%")
        .limit(10)
        .execute()
        .data
        or []
    )
    # Also match aliases or area if direct ilike missed
    all_custs = (
        sb.table("customers")
        .select("id,full_name,area_landmark,phone,zoho_contact_id,aliases")
        .eq("is_active", True)
        .limit(150)
        .execute()
        .data
        or []
    )
    seen_ids = {c["id"] for c in cust_rows}
    q_low = q.lower()
    ap_low = area_or_phone.strip().lower()
    for c in all_custs:
        if c["id"] in seen_ids:
            continue
        hay = " ".join(
            [
                c.get("full_name") or "",
                c.get("area_landmark") or "",
                c.get("phone") or "",
                *(c.get("aliases") or []),
            ]
        ).lower()
        if q_low in hay or (ap_low and ap_low in hay):
            cust_rows.append(c)
            seen_ids.add(c["id"])

    prj_rows = (
        sb.table("projects")
        .select("id,project_code,customer_id,customer_name,site_area_landmark,rough_scope,pipeline_stage,material_stage,payment_stage,status,quoted_total")
        .eq("project_type", "customer")
        .ilike("customer_name", f"%{q}%")
        .limit(12)
        .execute()
        .data
        or []
    )

    zoho_matches = await zoho.search_zoho_contacts(q, "customer")
    return {
        "customers": cust_rows[:8],
        "matching_projects": prj_rows[:8],
        "zoho_contacts": [
            {"contact_id": z.get("contact_id"), "contact_name": z.get("contact_name")}
            for z in zoho_matches[:5]
        ],
    }


def _tool_search_projects_and_items(
    sb: Any,
    query: str = "",
    project_id: str | None = None,
    include_completed: bool = False,
) -> dict[str, Any]:
    req = sb.table("projects").select(
        "id,project_code,customer_id,customer_name,site_area_landmark,rough_scope,"
        "pipeline_stage,material_stage,payment_stage,boq_scope_changed,status,"
        "promised_delivery_date,quoted_total,paid_cache_inr,estimator_ids,sizing_mistri_id,material_owner_id"
    ).eq("project_type", "customer")

    if project_id:
        req = req.eq("id", project_id)
    else:
        if not include_completed:
            req = req.eq("status", "active")
        if query.strip():
            # Fetch active and filter across name, code, scope, and item names
            pass

    projects = req.order("updated_at", desc=True).limit(60).execute().data or []
    q_low = query.strip().lower()

    p_ids = [p["id"] for p in projects]
    items: list[dict[str, Any]] = []
    if p_ids:
        items = (
            sb.table("project_items")
            .select("id,project_id,item_name,stage,dimensions,production_mistri_id,assembly_mistri_id,qc_passed_by_id,stage_entered_at")
            .in_("project_id", p_ids)
            .execute()
            .data
            or []
        )

    items_by_prj: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        items_by_prj.setdefault(it["project_id"], []).append(it)
    open_mats_by_prj: dict[str, list[dict[str, Any]]] = {}
    if p_ids:
        mats_raw = (
            sb.table("item_materials")
            .select("id,project_id,item_id,material_name,quantity,unit,status,category")
            .in_("project_id", p_ids)
            .in_("status", ["needed", "ordered"])
            .execute()
            .data
            or []
        )
        for mr in mats_raw:
            open_mats_by_prj.setdefault(mr["project_id"], []).append(mr)

    results: list[dict[str, Any]] = []
    for p in projects:
        p_items = items_by_prj.get(p["id"], [])
        if q_low and not project_id:
            hay = " ".join(
                [
                    p.get("project_code") or "",
                    p.get("customer_name") or "",
                    p.get("site_area_landmark") or "",
                    p.get("rough_scope") or "",
                    *[i.get("item_name") or "" for i in p_items],
                ]
            ).lower()
            if q_low not in hay:
                continue
        results.append({**p, "items": p_items, "open_materials": open_mats_by_prj.get(p["id"], [])[:20]})

    return {"projects": results[:12]}


async def _tool_search_people_and_vendors(sb: Any, query: str) -> dict[str, Any]:
    q_low = query.strip().lower()
    people = (
        sb.table("people")
        .select("id,full_name,person_type,role_tags,aliases,payout_aliases,linked_vendor_id,is_default_purchase,is_default_qc")
        .eq("is_active", True)
        .limit(150)
        .execute()
        .data
        or []
    )
    matched_people = [
        p
        for p in people
        if not q_low
        or q_low in (p.get("full_name") or "").lower()
        or any(q_low in a.lower() for a in (p.get("aliases") or []))
        or any(q_low in pa.lower() for pa in (p.get("payout_aliases") or []))
        or any(q_low in rt.lower() for rt in (p.get("role_tags") or []))
    ]

    vendors = (
        sb.table("vendors")
        .select("id,canonical_name,vendor_kind,specialty,aliases,zoho_contact_id")
        .eq("is_active", True)
        .limit(150)
        .execute()
        .data
        or []
    )
    matched_vendors = [
        v
        for v in vendors
        if not q_low
        or q_low in (v.get("canonical_name") or "").lower()
        or any(q_low in a.lower() for a in (v.get("aliases") or []))
        or q_low in (v.get("specialty") or "").lower()
    ]

    zoho_vendors = await zoho.search_zoho_contacts(query, "vendor") if q_low else []
    return {
        "people": matched_people[:10],
        "vendors": matched_vendors[:10],
        "zoho_vendors": [
            {"contact_id": zv.get("contact_id"), "contact_name": zv.get("contact_name")}
            for zv in zoho_vendors[:5]
        ],
    }


async def _tool_query_workshop_pulse(
    sb: Any, topic: str, filter_query: str = ""
) -> dict[str, Any]:
    fq = filter_query.strip().lower()
    if topic == "recent_transactions":
        txns = await zoho.list_recent_ledger_transactions(sb, limit=20, party_query=filter_query)
        return {"transactions": txns}
    if topic == "account_balances":
        accs = (
            sb.table("payment_accounts")
            .select("code,display_name,account_kind,cached_balance_inr,balance_synced_at")
            .eq("is_active", True)
            .order("sort_order")
            .execute()
            .data
            or []
        )
        if fq:
            accs = [a for a in accs if fq in a["display_name"].lower() or fq in a["code"].lower()]
        return {"payment_accounts": accs}

    if topic == "project_pnl":
        rows = (
            sb.table("v_project_pnl")
            .select("*")
            .eq("project_type", "customer")
            .limit(40)
            .execute()
            .data
            or []
        )
        if fq:
            rows = [r for r in rows if fq in (r.get("customer_name") or "").lower() or fq in (r.get("project_code") or "").lower()]
        return {"project_pnl": rows[:15]}

    if topic == "needs_info":
        rows = sb.table("v_needs_info_queue").select("*").limit(20).execute().data or []
        return {"needs_info_queue": rows}

    # Default: whos_doing_what
    wdw = step_executors.enrich_whos_doing_what_rows(
        sb, sb.table("v_whos_doing_what").select("*").limit(60).execute().data or []
    )
    people_map = {
        p["id"]: p["full_name"]
        for p in (sb.table("people").select("id,full_name").execute().data or [])
    }
    enriched = []
    for row in wdw:
        names = [people_map.get(pid, str(pid)) for pid in (row.get("assigned_person_ids") or [])]
        entry = {**row, "assigned_names": names}
        if fq:
            hay = f"{row.get('customer_name','')} {row.get('stage_label','')} {' '.join(names)}".lower()
            if fq not in hay:
                continue
        enriched.append(entry)
    return {"whos_doing_what": enriched[:25]}
