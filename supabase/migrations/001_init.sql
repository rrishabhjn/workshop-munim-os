-- =============================================================================
-- Workshop Munim OS — Initial Schema (001_init.sql)
-- Target: Supabase PostgreSQL (ap-south-1 Mumbai)
-- Security invariants:
--   - RLS enabled on every table in public
--   - Views defined WITH (security_invoker = true)
--   - Policies use TO authenticated + app_users email allowlist (no auth.role(), no user_metadata)
--   - No SECURITY DEFINER functions in public
--   - No rigid ENUM types for workshop stages or material categories
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- 1. Trigger helper for updated_at (SECURITY INVOKER)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- -----------------------------------------------------------------------------
-- 2. Lookup Tables (Row-extensible instead of Postgres ENUMs)
-- -----------------------------------------------------------------------------
CREATE TABLE public.workshop_stages (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.workshop_stages (code, label, sort_order, is_terminal) VALUES
    ('design_measurement', 'Design & Measurement', 1, FALSE),
    ('material_pending',   'Material Pending',     2, FALSE),
    ('carpentry',          'Carpentry',            3, FALSE),
    ('polish_upholstery',  'Polish / Upholstery',  4, FALSE),
    ('ready',              'Ready',                5, FALSE),
    ('delivered',          'Delivered',            6, TRUE);

CREATE TABLE public.material_categories (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.material_categories (code, label, sort_order) VALUES
    ('ply_wood',    'Ply / Wood',         1),
    ('laminate',    'Laminate / Sunmica', 2),
    ('hardware',    'Hardware',           3),
    ('foam_fabric', 'Foam / Fabric',      4),
    ('polish',      'Polish / Adhesive',  5);

-- -----------------------------------------------------------------------------
-- 3. Allowlist & Identity Mapping
-- -----------------------------------------------------------------------------
CREATE TABLE public.app_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE,
    telegram_user_id BIGINT UNIQUE,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff' CHECK (role IN ('owner', 'staff', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_app_users_email_lower ON public.app_users (lower(email));
CREATE INDEX idx_app_users_telegram_id ON public.app_users (telegram_user_id);

-- -----------------------------------------------------------------------------
-- 4. Core Operational Tables
-- -----------------------------------------------------------------------------
CREATE TABLE public.projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_code TEXT NOT NULL UNIQUE,
    customer_name TEXT NOT NULL,
    phone TEXT,
    zoho_contact_id TEXT,
    zoho_project_id TEXT,
    quoted_total NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    gst_flag BOOLEAN NOT NULL DEFAULT TRUE,
    promised_delivery_date DATE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'on_hold')),
    zoho_invoiced_cache NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    zoho_bills_cache NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    zoho_synced_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projects_status_delivery ON public.projects (status, promised_delivery_date);
CREATE INDEX idx_projects_customer_name ON public.projects (customer_name);

CREATE TRIGGER trg_projects_updated_at
    BEFORE UPDATE ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.project_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    category TEXT,
    dimensions TEXT,
    specs_notes TEXT,
    stage TEXT NOT NULL DEFAULT 'design_measurement' REFERENCES public.workshop_stages(code),
    assigned_karigar TEXT,
    target_date DATE,
    photo_paths TEXT[] NOT NULL DEFAULT '{}'::text[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_project_items_project_id ON public.project_items (project_id);
CREATE INDEX idx_project_items_stage ON public.project_items (stage);
CREATE INDEX idx_project_items_target_date ON public.project_items (target_date);

CREATE TRIGGER trg_project_items_updated_at
    BEFORE UPDATE ON public.project_items
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.item_materials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    item_id UUID NOT NULL REFERENCES public.project_items(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT 'ply_wood' REFERENCES public.material_categories(code),
    material_name TEXT NOT NULL,
    quantity NUMERIC(10,2) NOT NULL DEFAULT 1.00 CHECK (quantity > 0),
    unit TEXT NOT NULL DEFAULT 'pcs',
    status TEXT NOT NULL DEFAULT 'needed' CHECK (status IN ('needed', 'ordered', 'in_shop')),
    preferred_supplier TEXT,
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_item_materials_status ON public.item_materials (status);
CREATE INDEX idx_item_materials_item_id ON public.item_materials (item_id);
CREATE INDEX idx_item_materials_project_id ON public.item_materials (project_id);

CREATE TRIGGER trg_item_materials_updated_at
    BEFORE UPDATE ON public.item_materials
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.cash_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    txn_date DATE NOT NULL DEFAULT CURRENT_DATE,
    txn_type TEXT NOT NULL CHECK (txn_type IN ('cash_advance', 'cash_balance', 'karigar_labor', 'local_transport', 'misc')),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    party_or_karigar TEXT NOT NULL,
    project_id UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    item_id UUID REFERENCES public.project_items(id) ON DELETE SET NULL,
    amount_inr NUMERIC(12,2) NOT NULL CHECK (amount_inr >= 0),
    payment_mode TEXT NOT NULL DEFAULT 'cash',
    notes TEXT,
    logged_by TEXT,
    verified_by_staff BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cash_ledger_txn_date ON public.cash_ledger (txn_date DESC);
CREATE INDEX idx_cash_ledger_project_id ON public.cash_ledger (project_id);
CREATE INDEX idx_cash_ledger_verified ON public.cash_ledger (verified_by_staff);

CREATE TABLE public.followups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    due_date DATE NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'customer' CHECK (entity_type IN ('customer', 'supplier', 'karigar', 'internal')),
    name TEXT NOT NULL,
    phone TEXT,
    project_id UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    action_note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    source_msg_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_followups_status_due ON public.followups (status, due_date);

CREATE TRIGGER trg_followups_updated_at
    BEFORE UPDATE ON public.followups
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.entity_dictionary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('customer', 'supplier', 'karigar', 'material')),
    aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    phone TEXT,
    zoho_contact_id TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_entity_dictionary_type ON public.entity_dictionary (entity_type);
CREATE INDEX idx_entity_dictionary_aliases ON public.entity_dictionary USING GIN (aliases);

CREATE TRIGGER trg_entity_dictionary_updated_at
    BEFORE UPDATE ON public.entity_dictionary
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.historical_archive (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_type TEXT NOT NULL CHECK (doc_type IN ('supplier_bill', 'customer_invoice', 'estimate', 'receipt', 'other')),
    doc_date DATE,
    party_name TEXT NOT NULL,
    invoice_no TEXT,
    amount_inr NUMERIC(12,2),
    gst_amount NUMERIC(12,2),
    paid_status TEXT NOT NULL DEFAULT 'unknown',
    storage_path TEXT NOT NULL,
    extracted_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(party_name, '') || ' ' ||
            coalesce(invoice_no, '') || ' ' ||
            coalesce(doc_type, '')
        )
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_historical_archive_party_inv ON public.historical_archive (party_name, invoice_no);
CREATE INDEX idx_historical_archive_doc_date ON public.historical_archive (doc_date DESC);
CREATE INDEX idx_historical_archive_search ON public.historical_archive USING GIN (search_vector);

CREATE TABLE public.telegram_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_update_id BIGINT NOT NULL UNIQUE,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    media_group_id TEXT,
    pending_actions JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_telegram_state_media_group ON public.telegram_state (media_group_id) WHERE media_group_id IS NOT NULL;
CREATE INDEX idx_telegram_state_status ON public.telegram_state (status, expires_at);

-- -----------------------------------------------------------------------------
-- 5. SQL Views (security_invoker = true)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_master_purchase_list
WITH (security_invoker = true) AS
SELECT
    im.category,
    mc.label AS category_label,
    im.material_name,
    im.unit,
    SUM(im.quantity) AS total_qty_needed,
    COUNT(*)::INTEGER AS item_count,
    jsonb_agg(
        jsonb_build_object(
            'material_id', im.id,
            'project_id', p.id,
            'project_code', p.project_code,
            'customer_name', p.customer_name,
            'item_id', pi.id,
            'item_name', pi.item_name,
            'quantity', im.quantity,
            'preferred_supplier', im.preferred_supplier,
            'notes', im.notes
        )
        ORDER BY p.promised_delivery_date NULLS LAST, p.customer_name, pi.item_name
    ) AS customer_breakdown
FROM public.item_materials im
JOIN public.material_categories mc ON mc.code = im.category
JOIN public.projects p ON p.id = im.project_id
JOIN public.project_items pi ON pi.id = im.item_id
WHERE im.status = 'needed'
  AND p.status = 'active'
GROUP BY im.category, mc.label, mc.sort_order, im.material_name, im.unit
ORDER BY mc.sort_order, im.material_name;

CREATE OR REPLACE VIEW public.v_project_pnl
WITH (security_invoker = true) AS
WITH cash_agg AS (
    SELECT
        project_id,
        COALESCE(SUM(CASE WHEN direction = 'in' THEN amount_inr ELSE 0 END), 0.00)::NUMERIC(12,2) AS cash_in,
        COALESCE(SUM(CASE WHEN direction = 'out' THEN amount_inr ELSE 0 END), 0.00)::NUMERIC(12,2) AS cash_out
    FROM public.cash_ledger
    WHERE project_id IS NOT NULL
    GROUP BY project_id
)
SELECT
    p.id AS project_id,
    p.project_code,
    p.customer_name,
    p.status,
    p.promised_delivery_date,
    p.quoted_total,
    p.zoho_invoiced_cache AS formal_invoiced,
    COALESCE(c.cash_in, 0.00)::NUMERIC(12,2) AS cash_in,
    (p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00))::NUMERIC(12,2) AS total_revenue,
    p.zoho_bills_cache AS formal_bills,
    COALESCE(c.cash_out, 0.00)::NUMERIC(12,2) AS cash_out,
    (p.zoho_bills_cache + COALESCE(c.cash_out, 0.00))::NUMERIC(12,2) AS total_cost,
    ((p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00)) - (p.zoho_bills_cache + COALESCE(c.cash_out, 0.00)))::NUMERIC(12,2) AS net_profit,
    CASE
        WHEN (p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00)) > 0
        THEN ROUND(
            (
                ((p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00)) - (p.zoho_bills_cache + COALESCE(c.cash_out, 0.00)))
                / (p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00))
            ) * 100.0,
            2
        )
        ELSE 0.00
    END AS margin_pct,
    GREATEST(p.quoted_total - (p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00)), 0.00)::NUMERIC(12,2) AS balance_pending
FROM public.projects p
LEFT JOIN cash_agg c ON c.project_id = p.id;

-- -----------------------------------------------------------------------------
-- 6. Row Level Security (RLS) — Enabled on EVERY public table
-- -----------------------------------------------------------------------------
ALTER TABLE public.workshop_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.material_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.item_materials ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cash_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.followups ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.entity_dictionary ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.historical_archive ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_state ENABLE ROW LEVEL SECURITY;

-- Lookup tables: allowlisted authenticated users can read
CREATE POLICY workshop_stages_select ON public.workshop_stages
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY material_categories_select ON public.material_categories
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- app_users: allowlisted authenticated users can read active roster
CREATE POLICY app_users_select ON public.app_users
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- projects
CREATE POLICY projects_select ON public.projects
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY projects_insert ON public.projects
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY projects_update ON public.projects
    FOR UPDATE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- project_items
CREATE POLICY project_items_select ON public.project_items
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY project_items_insert ON public.project_items
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY project_items_update ON public.project_items
    FOR UPDATE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- item_materials
CREATE POLICY item_materials_select ON public.item_materials
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY item_materials_insert ON public.item_materials
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY item_materials_update ON public.item_materials
    FOR UPDATE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- cash_ledger
CREATE POLICY cash_ledger_select ON public.cash_ledger
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY cash_ledger_insert ON public.cash_ledger
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY cash_ledger_update ON public.cash_ledger
    FOR UPDATE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- followups
CREATE POLICY followups_select ON public.followups
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY followups_insert ON public.followups
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY followups_update ON public.followups
    FOR UPDATE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- entity_dictionary
CREATE POLICY entity_dictionary_select ON public.entity_dictionary
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY entity_dictionary_insert ON public.entity_dictionary
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY entity_dictionary_update ON public.entity_dictionary
    FOR UPDATE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- historical_archive
CREATE POLICY historical_archive_select ON public.historical_archive
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY historical_archive_insert ON public.historical_archive
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

-- telegram_state: intentionally has RLS enabled with NO authenticated policies.
-- Accessed strictly server-side via SUPABASE_SECRET_KEY (service_role).
