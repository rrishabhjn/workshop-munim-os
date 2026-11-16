-- =============================================================================
-- Workshop Munim OS — Phase 1: Hardened Multi-Domain Spine, Lifecycle & AgentCore
-- File: supabase/migrations/006_unified_multi_domain_and_agent.sql
-- Target: Supabase PostgreSQL (ap-south-1 Mumbai)
--
-- Security & Architecture Invariants Enforced:
--   1. RLS enabled on EVERY table in public
--   2. Every SQL view defined WITH (security_invoker = true)
--   3. Policies use TO authenticated + active app_users allowlist check
--      (never auth.role(), never user_metadata)
--   4. Every UPDATE policy includes BOTH USING and WITH CHECK + matching SELECT
--   5. Zero SECURITY DEFINER functions in public
--   6. Zero rigid Postgres ENUM types (row-extensible lookup tables)
--   7. NUMERIC(12,2) on all monetary columns + metadata JSONB NOT NULL DEFAULT '{}'
--   8. Bakes in all 6 AI/Systems Hardenings + Zoho Standard Reporting Tag fields
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -----------------------------------------------------------------------------
-- 1. Safe Purge of Pre-Launch Dummy Operational Rows
--    (Strictly preserves app_users, workshop_memory, historical_archive & GEN-STOCK)
-- -----------------------------------------------------------------------------
TRUNCATE TABLE
    public.item_materials,
    public.item_parts,
    public.project_items,
    public.cash_ledger,
    public.followups,
    public.telegram_state,
    public.entity_dictionary
RESTART IDENTITY CASCADE;

DELETE FROM public.projects
WHERE project_code <> 'GEN-STOCK';

-- -----------------------------------------------------------------------------
-- 2. Row-Extensible Lifecycle Lookup Tables (No Postgres ENUMs)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.project_pipeline_stages (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.project_pipeline_stages (code, label, sort_order, is_terminal) VALUES
    ('lead',                    'Lead',                                  1, FALSE),
    ('design',                  'Design',                                2, FALSE),
    ('estimating',              'Estimating',                            3, FALSE),
    ('quoted_awaiting_advance', 'Quoted to Customer / Awaiting Advance', 4, FALSE),
    ('site_sizing_boq',         'Site Sizing / BOQ',                     5, FALSE),
    ('execution',               'Execution',                             6, FALSE),
    ('completed',               'Completed',                             7, TRUE),
    ('on_hold',                 'On Hold',                               8, FALSE),
    ('lost',                    'Lost / Dropped',                        9, TRUE)
ON CONFLICT (code) DO UPDATE
SET label = EXCLUDED.label,
    sort_order = EXCLUDED.sort_order,
    is_terminal = EXCLUDED.is_terminal;

CREATE TABLE IF NOT EXISTS public.material_track_stages (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.material_track_stages (code, label, sort_order, is_terminal) VALUES
    ('not_started',     'Not Started',       1, FALSE),
    ('check_stock',     'Check Stock',       2, FALSE),
    ('ordered_pending', 'Ordered (Pending)', 3, FALSE),
    ('all_in_shop',     'All In Shop',       4, TRUE)
ON CONFLICT (code) DO UPDATE
SET label = EXCLUDED.label,
    sort_order = EXCLUDED.sort_order,
    is_terminal = EXCLUDED.is_terminal;

CREATE TABLE IF NOT EXISTS public.payment_track_stages (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.payment_track_stages (code, label, sort_order, is_terminal) VALUES
    ('advance_pending',  'Advance Pending',        1, FALSE),
    ('advance_in',       'Advance In',             2, FALSE),
    ('partial_paid',     'Partial / Pre-Dispatch', 3, FALSE),
    ('payment_complete', 'Payment Complete',       4, TRUE)
ON CONFLICT (code) DO UPDATE
SET label = EXCLUDED.label,
    sort_order = EXCLUDED.sort_order,
    is_terminal = EXCLUDED.is_terminal;

-- Re-seed workshop_stages cleanly to the 4 physical-location Item Execution stages
ALTER TABLE public.project_items ALTER COLUMN stage SET DEFAULT 'in_shop';
ALTER TABLE public.item_parts ALTER COLUMN stage SET DEFAULT 'in_shop';

DELETE FROM public.workshop_stages;

INSERT INTO public.workshop_stages (code, label, sort_order, is_terminal) VALUES
    ('in_shop',      'In Shop',        1, FALSE),
    ('packed_ready', 'Packed / Ready', 2, FALSE),
    ('at_site',      'At Site',        3, FALSE),
    ('qc_done',      'QC Done',        4, TRUE);

-- -----------------------------------------------------------------------------
-- 3. Shared Master Data Spine (Customers, Vendors, People, Payment Accounts)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name TEXT NOT NULL,
    area_landmark TEXT,
    phone TEXT,
    zoho_contact_id TEXT UNIQUE,
    aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    notes TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_customers_name_trgm
    ON public.customers USING GIN (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_customers_area_trgm
    ON public.customers USING GIN (area_landmark gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_customers_aliases_gin
    ON public.customers USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_customers_phone
    ON public.customers (phone) WHERE phone IS NOT NULL;

DROP TRIGGER IF EXISTS trg_customers_updated_at ON public.customers;
CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON public.customers
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL,
    vendor_kind TEXT NOT NULL DEFAULT 'supplier'
        CHECK (vendor_kind IN ('supplier', 'contractor')),
    phone TEXT,
    gstin TEXT,
    zoho_contact_id TEXT UNIQUE,
    aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    specialty TEXT,
    notes TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vendors_name_trgm
    ON public.vendors USING GIN (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_vendors_aliases_gin
    ON public.vendors USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_vendors_kind
    ON public.vendors (vendor_kind, is_active);

DROP TRIGGER IF EXISTS trg_vendors_updated_at ON public.vendors;
CREATE TRIGGER trg_vendors_updated_at
    BEFORE UPDATE ON public.vendors
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.people (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_user_id UUID UNIQUE REFERENCES public.app_users(id) ON DELETE SET NULL,
    linked_vendor_id UUID REFERENCES public.vendors(id) ON DELETE SET NULL,
    full_name TEXT NOT NULL,
    person_type TEXT NOT NULL DEFAULT 'salaried_karigar'
        CHECK (person_type IN ('operator', 'salaried_karigar', 'contractor_mistri')),
    role_tags TEXT[] NOT NULL DEFAULT '{}'::text[],
    aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    payout_aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    email TEXT UNIQUE,
    telegram_user_id BIGINT UNIQUE,
    phone TEXT,
    can_login_prod BOOLEAN NOT NULL DEFAULT FALSE,
    can_login_munim BOOLEAN NOT NULL DEFAULT FALSE,
    is_default_purchase BOOLEAN NOT NULL DEFAULT FALSE,
    is_default_qc BOOLEAN NOT NULL DEFAULT FALSE,
    monthly_salary_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (monthly_salary_inr >= 0),
    standard_shift_end_local TIME NOT NULL DEFAULT '18:00:00',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_people_name_trgm
    ON public.people USING GIN (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_people_aliases_gin
    ON public.people USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_people_payout_aliases_gin
    ON public.people USING GIN (payout_aliases);
CREATE INDEX IF NOT EXISTS idx_people_type_active
    ON public.people (person_type, is_active);
CREATE INDEX IF NOT EXISTS idx_people_telegram_id
    ON public.people (telegram_user_id) WHERE telegram_user_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_people_updated_at ON public.people;
CREATE TRIGGER trg_people_updated_at
    BEFORE UPDATE ON public.people
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Auto-bridge existing allowlisted app_users into people + ensure Ansh default exists
INSERT INTO public.people (
    app_user_id,
    full_name,
    person_type,
    role_tags,
    email,
    telegram_user_id,
    can_login_prod,
    can_login_munim,
    is_default_qc,
    is_active
)
SELECT
    au.id,
    au.full_name,
    'operator',
    CASE
        WHEN au.role = 'owner' THEN ARRAY['owner', 'qc']
        WHEN au.role = 'admin' THEN ARRAY['admin']
        ELSE ARRAY['clerk']
    END,
    au.email,
    au.telegram_user_id,
    TRUE,
    TRUE,
    (au.role = 'owner'),
    au.is_active
FROM public.app_users au
ON CONFLICT (app_user_id) DO NOTHING;

INSERT INTO public.people (
    full_name,
    person_type,
    role_tags,
    aliases,
    can_login_prod,
    can_login_munim,
    is_default_purchase,
    is_active
)
SELECT
    'Ansh',
    'operator',
    ARRAY['purchase', 'inventory'],
    ARRAY['Ansh Ji', 'Ansh Bhaiya'],
    TRUE,
    TRUE,
    TRUE,
    TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM public.people WHERE lower(full_name) = 'ansh'
);

CREATE TABLE IF NOT EXISTS public.payment_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    account_kind TEXT NOT NULL
        CHECK (account_kind IN ('bank', 'od_cc', 'cash_galla', 'supervisor_float', 'wallet')),
    aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    zoho_account_id TEXT UNIQUE,
    custodian_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    cached_balance_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    balance_synced_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payment_accounts_aliases_gin
    ON public.payment_accounts USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_payment_accounts_kind
    ON public.payment_accounts (account_kind, is_active, sort_order);

DROP TRIGGER IF EXISTS trg_payment_accounts_updated_at ON public.payment_accounts;
CREATE TRIGGER trg_payment_accounts_updated_at
    BEFORE UPDATE ON public.payment_accounts
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

INSERT INTO public.payment_accounts (code, display_name, account_kind, aliases, sort_order) VALUES
    ('cash_galla',    'Cash Galla',          'cash_galla',       ARRAY['cash', 'galla', 'shop cash', 'nakad'], 1),
    ('axis',          'Axis Bank',           'bank',             ARRAY['axis', 'axis bank'],                   2),
    ('rgb',           'RGB (Gramin Bank)',   'bank',             ARRAY['rgb', 'rajasthan gramin bank'],        3),
    ('ccrgb',         'CCRGB (OD / CC)',     'od_cc',            ARRAY['ccrgb', 'cc rgb', 'od'],               4),
    ('icici',         'ICICI Bank',          'bank',             ARRAY['icici', 'icici bank'],                 5),
    ('idfc',          'IDFC First Bank',     'bank',             ARRAY['idfc', 'idfc bank'],                   6),
    ('sbi',           'SBI',                 'bank',             ARRAY['sbi', 'state bank'],                   7),
    ('canara',        'Canara Bank',         'bank',             ARRAY['canara', 'canara bank'],               8),
    ('paytm',         'Paytm / UPI',         'wallet',           ARRAY['paytm', 'upi', 'qr'],                  9),
    ('float_prakash', 'Prakash Ji Float',    'supervisor_float', ARRAY['prakash', 'prakash chand', 'prakash ji'], 10),
    ('float_kamlesh', 'Kamlesh Bhaiya Float','supervisor_float', ARRAY['kamlesh', 'kamlesh bhaiya', 'kamlesh ji'], 11),
    ('float_manu',    'Manu Bhaiya Wallet',  'supervisor_float', ARRAY['manu', 'manu bhaiya'],                 12),
    ('float_ansh',    'Ansh Wallet',         'supervisor_float', ARRAY['ansh float', 'ansh wallet', 'ansh cash'], 13)
ON CONFLICT (code) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 4. Two-Tier Production Lifecycle + Zoho Standard Tagging on Projects & Items
-- -----------------------------------------------------------------------------
ALTER TABLE public.projects
    DROP CONSTRAINT IF EXISTS projects_status_check;

ALTER TABLE public.projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN ('active', 'completed', 'on_hold', 'lost'));

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES public.customers(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS site_area_landmark TEXT,
    ADD COLUMN IF NOT EXISTS rough_scope TEXT,
    ADD COLUMN IF NOT EXISTS pipeline_stage TEXT NOT NULL DEFAULT 'lead'
        REFERENCES public.project_pipeline_stages(code),
    ADD COLUMN IF NOT EXISTS material_stage TEXT NOT NULL DEFAULT 'not_started'
        REFERENCES public.material_track_stages(code),
    ADD COLUMN IF NOT EXISTS payment_stage TEXT NOT NULL DEFAULT 'advance_pending'
        REFERENCES public.payment_track_stages(code),
    ADD COLUMN IF NOT EXISTS boq_scope_changed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS boq_scope_note TEXT,
    ADD COLUMN IF NOT EXISTS designed_by_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS estimator_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    ADD COLUMN IF NOT EXISTS current_owner_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    ADD COLUMN IF NOT EXISTS sizing_mistri_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS material_owner_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS pipeline_stage_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS material_stage_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS payment_stage_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS zoho_tag_id TEXT,
    ADD COLUMN IF NOT EXISTS zoho_tag_option_id TEXT,
    ADD COLUMN IF NOT EXISTS cf_project_code TEXT,
    ADD COLUMN IF NOT EXISTS paid_cache_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    ADD COLUMN IF NOT EXISTS cost_cache_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00;

CREATE INDEX IF NOT EXISTS idx_projects_pipeline_stage
    ON public.projects (pipeline_stage, pipeline_stage_at);
CREATE INDEX IF NOT EXISTS idx_projects_material_stage
    ON public.projects (material_stage, material_owner_id);
CREATE INDEX IF NOT EXISTS idx_projects_customer_id
    ON public.projects (customer_id);
CREATE INDEX IF NOT EXISTS idx_projects_current_owners_gin
    ON public.projects USING GIN (current_owner_ids);
CREATE INDEX IF NOT EXISTS idx_projects_estimators_gin
    ON public.projects USING GIN (estimator_ids);

CREATE OR REPLACE FUNCTION public.sync_project_lifecycle_timestamps()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.cf_project_code := COALESCE(NEW.cf_project_code, NEW.project_code);
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.pipeline_stage IS DISTINCT FROM OLD.pipeline_stage THEN
            NEW.pipeline_stage_at := now();
        END IF;
        IF NEW.material_stage IS DISTINCT FROM OLD.material_stage THEN
            NEW.material_stage_at := now();
        END IF;
        IF NEW.payment_stage IS DISTINCT FROM OLD.payment_stage THEN
            NEW.payment_stage_at := now();
        END IF;
    END IF;

    IF NEW.project_type = 'customer' THEN
        IF NEW.pipeline_stage = 'completed' THEN
            NEW.status := 'completed';
        ELSIF NEW.pipeline_stage = 'on_hold' THEN
            NEW.status := 'on_hold';
        ELSIF NEW.pipeline_stage = 'lost' THEN
            NEW.status := 'lost';
        ELSE
            NEW.status := 'active';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_projects_lifecycle_sync ON public.projects;
CREATE TRIGGER trg_projects_lifecycle_sync
    BEFORE INSERT OR UPDATE ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public.sync_project_lifecycle_timestamps();

ALTER TABLE public.project_items
    ADD COLUMN IF NOT EXISTS production_mistri_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS assembly_mistri_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS qc_passed_by_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS qc_passed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stage_entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS pre_pack_photo_path TEXT;

CREATE INDEX IF NOT EXISTS idx_project_items_prod_mistri
    ON public.project_items (production_mistri_id) WHERE production_mistri_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_project_items_assy_mistri
    ON public.project_items (assembly_mistri_id) WHERE assembly_mistri_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_project_items_stage_entered
    ON public.project_items (stage, stage_entered_at);

CREATE OR REPLACE FUNCTION public.sync_item_stage_timestamps()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.stage IS DISTINCT FROM OLD.stage THEN
        NEW.stage_entered_at := now();
        IF NEW.stage = 'qc_done' AND NEW.qc_passed_at IS NULL THEN
            NEW.qc_passed_at := now();
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_project_items_stage_sync ON public.project_items;
CREATE TRIGGER trg_project_items_stage_sync
    BEFORE UPDATE ON public.project_items
    FOR EACH ROW EXECUTE FUNCTION public.sync_item_stage_timestamps();

-- Immutable Audit Log + Reversible 1-Tap [Undo] Snapshots + Audio Transcript Echo
CREATE TABLE IF NOT EXISTS public.stage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    item_id UUID REFERENCES public.project_items(id) ON DELETE CASCADE,
    track TEXT NOT NULL CHECK (track IN ('pipeline', 'material', 'payment', 'item')),
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    prior_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    assigned_people_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    actor_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    source_channel TEXT NOT NULL DEFAULT 'web'
        CHECK (source_channel IN ('web', 'telegram', 'system', 'undo')),
    heard_transcript TEXT,
    duration_hours NUMERIC(10,2),
    note TEXT,
    is_undone BOOLEAN NOT NULL DEFAULT FALSE,
    undone_at TIMESTAMPTZ,
    undone_by_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stage_events_project_created
    ON public.stage_events (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stage_events_item_created
    ON public.stage_events (item_id, created_at DESC) WHERE item_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 5. All 6 AI/Systems Hardenings: Webhook Dedup, 2.5s Burst Buffer & Agent Goals
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.telegram_webhook_dedup (
    telegram_update_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    user_id BIGINT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.telegram_inbox_buffer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_update_id BIGINT NOT NULL UNIQUE
        REFERENCES public.telegram_webhook_dedup(telegram_update_id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    domain_channel TEXT NOT NULL DEFAULT 'dm'
        CHECK (domain_channel IN ('production', 'munim', 'attendance', 'dm')),
    message_id BIGINT,
    reply_to_message_id BIGINT,
    media_group_id TEXT,
    raw_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'buffered'
        CHECK (status IN ('buffered', 'claimed', 'processed', 'ignored', 'failed')),
    claim_token UUID,
    claimed_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telegram_inbox_chat_user_buffered
    ON public.telegram_inbox_buffer (chat_id, user_id, created_at)
    WHERE status = 'buffered';

-- Per-(chat_id, user_id) transaction-scoped advisory lock helper (SECURITY INVOKER)
CREATE OR REPLACE FUNCTION public.try_lock_user_inbox(p_chat_id BIGINT, p_user_id BIGINT)
RETURNS BOOLEAN
LANGUAGE sql
AS $$
    SELECT pg_try_advisory_xact_lock(
        hashtextextended(p_chat_id::text || ':' || p_user_id::text, 0)
    );
$$;

-- Canonical Omnichannel AI State Checklist (Zero Silent Expiry — Hardening #1..#6)
CREATE TABLE IF NOT EXISTS public.agent_goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    source_channel TEXT NOT NULL DEFAULT 'telegram'
        CHECK (source_channel IN ('telegram', 'web_command_bar', 'system')),
    domain_scope TEXT NOT NULL DEFAULT 'production'
        CHECK (domain_scope IN ('production', 'munim', 'attendance', 'shared')),
    telegram_chat_id BIGINT,
    telegram_bot_msg_id BIGINT,
    ui_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    goal_type TEXT NOT NULL
        CHECK (goal_type IN (
            'new_lead',
            'stage_update',
            'assign_owner',
            'financial_txn',
            'supplier_bill',
            'float_transfer',
            'salary_advance',
            'entity_onboarding',
            'query',
            'repair'
        )),
    status TEXT NOT NULL DEFAULT 'gathering_info'
        CHECK (status IN (
            'gathering_info',
            'awaiting_confirmation',
            'executing',
            'needs_error_fix',
            'parked_for_clerk',
            'completed',
            'undone',
            'discarded'
        )),
    thinking_level_used TEXT NOT NULL DEFAULT 'low'
        CHECK (thinking_level_used IN ('minimal', 'low', 'medium', 'high', 'xhigh')),
    heard_transcript TEXT,
    summary_title TEXT NOT NULL DEFAULT '',
    collected_slots JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_slots TEXT[] NOT NULL DEFAULT '{}'::text[],
    pending_prompt TEXT,
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    repair_options JSONB NOT NULL DEFAULT '[]'::jsonb,
    turn_digest JSONB NOT NULL DEFAULT '[]'::jsonb,
    within_turn_interaction_id TEXT,
    undo_event_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    project_id UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    customer_id UUID REFERENCES public.customers(id) ON DELETE SET NULL,
    lock_version INTEGER NOT NULL DEFAULT 0,
    resolved_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_goals_open_status
    ON public.agent_goals (status, domain_scope, updated_at DESC)
    WHERE status IN ('gathering_info', 'awaiting_confirmation', 'needs_error_fix', 'parked_for_clerk');
CREATE INDEX IF NOT EXISTS idx_agent_goals_owner_open
    ON public.agent_goals (owner_person_id, created_at DESC)
    WHERE status IN ('gathering_info', 'awaiting_confirmation', 'needs_error_fix');
CREATE INDEX IF NOT EXISTS idx_agent_goals_tg_msg
    ON public.agent_goals (telegram_chat_id, telegram_bot_msg_id)
    WHERE telegram_bot_msg_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_agent_goals_updated_at ON public.agent_goals;
CREATE TRIGGER trg_agent_goals_updated_at
    BEFORE UPDATE ON public.agent_goals
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- -----------------------------------------------------------------------------
-- 6. Domain 2 HR Tables: Worker Photo Attendance (>6 PM OT) & Monthly FnF Payroll
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.worker_attendance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES public.people(id) ON DELETE CASCADE,
    work_date DATE NOT NULL,
    check_in_at TIMESTAMPTZ,
    check_out_at TIMESTAMPTZ,
    in_photo_path TEXT,
    out_photo_path TEXT,
    in_telegram_msg_id BIGINT,
    out_telegram_msg_id BIGINT,
    worked_minutes INTEGER NOT NULL DEFAULT 0 CHECK (worked_minutes >= 0),
    overtime_minutes INTEGER NOT NULL DEFAULT 0 CHECK (overtime_minutes >= 0),
    attendance_status TEXT NOT NULL DEFAULT 'present'
        CHECK (attendance_status IN ('present', 'missing_checkout', 'half_day', 'absent', 'paid_off')),
    decision_confidence NUMERIC(5,4),
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person_id, work_date)
);

CREATE INDEX IF NOT EXISTS idx_worker_attendance_date
    ON public.worker_attendance (work_date DESC, person_id);

DROP TRIGGER IF EXISTS trg_worker_attendance_updated_at ON public.worker_attendance;
CREATE TRIGGER trg_worker_attendance_updated_at
    BEFORE UPDATE ON public.worker_attendance
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.monthly_fnf_sheets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES public.people(id) ON DELETE CASCADE,
    salary_month TEXT NOT NULL CHECK (salary_month ~ '^[0-9]{4}-[0-9]{2}$'),
    days_present NUMERIC(5,2) NOT NULL DEFAULT 0.00,
    total_worked_hours NUMERIC(8,2) NOT NULL DEFAULT 0.00,
    total_overtime_hours NUMERIC(8,2) NOT NULL DEFAULT 0.00,
    base_salary_snapshot_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    suggested_gross_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    approved_gross_inr NUMERIC(12,2),
    advances_deducted_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    net_payable_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'approved', 'paid')),
    approved_by_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person_id, salary_month)
);

CREATE INDEX IF NOT EXISTS idx_monthly_fnf_month_status
    ON public.monthly_fnf_sheets (salary_month DESC, status);

DROP TRIGGER IF EXISTS trg_monthly_fnf_sheets_updated_at ON public.monthly_fnf_sheets;
CREATE TRIGGER trg_monthly_fnf_sheets_updated_at
    BEFORE UPDATE ON public.monthly_fnf_sheets
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.worker_payroll_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES public.people(id) ON DELETE CASCADE,
    salary_month TEXT NOT NULL CHECK (salary_month ~ '^[0-9]{4}-[0-9]{2}$'),
    txn_date DATE NOT NULL DEFAULT CURRENT_DATE,
    entry_type TEXT NOT NULL
        CHECK (entry_type IN ('advance', 'fnf_settlement', 'bonus', 'deduction')),
    amount_inr NUMERIC(12,2) NOT NULL CHECK (amount_inr >= 0),
    payment_account_id UUID REFERENCES public.payment_accounts(id) ON DELETE SET NULL,
    paid_via_alias TEXT,
    zoho_txn_id TEXT,
    zoho_reference_number TEXT UNIQUE,
    notes TEXT,
    logged_by_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_worker_payroll_person_month
    ON public.worker_payroll_entries (person_id, salary_month DESC, txn_date DESC);

DROP TRIGGER IF EXISTS trg_worker_payroll_entries_updated_at ON public.worker_payroll_entries;
CREATE TRIGGER trg_worker_payroll_entries_updated_at
    BEFORE UPDATE ON public.worker_payroll_entries
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- -----------------------------------------------------------------------------
-- 7. SQL Views (Strictly WITH (security_invoker = true))
-- -----------------------------------------------------------------------------
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
),
effective_totals AS (
    SELECT
        p.*,
        GREATEST(p.paid_cache_inr, p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00))::NUMERIC(12,2) AS rev_inr,
        GREATEST(p.cost_cache_inr, p.zoho_bills_cache + COALESCE(c.cash_out, 0.00))::NUMERIC(12,2) AS cost_inr,
        COALESCE(c.cash_in, 0.00)::NUMERIC(12,2) AS legacy_cash_in,
        COALESCE(c.cash_out, 0.00)::NUMERIC(12,2) AS legacy_cash_out
    FROM public.projects p
    LEFT JOIN cash_agg c ON c.project_id = p.id
)
SELECT
    et.id AS project_id,
    et.project_code,
    et.customer_name,
    et.status,
    et.promised_delivery_date,
    et.quoted_total,
    et.zoho_invoiced_cache AS formal_invoiced,
    et.legacy_cash_in AS cash_in,
    et.rev_inr AS total_revenue,
    et.zoho_bills_cache AS formal_bills,
    et.legacy_cash_out AS cash_out,
    et.cost_inr AS total_cost,
    (et.rev_inr - et.cost_inr)::NUMERIC(12,2) AS net_profit,
    CASE
        WHEN et.rev_inr > 0
        THEN ROUND(((et.rev_inr - et.cost_inr) / et.rev_inr) * 100.0, 2)
        ELSE 0.00
    END AS margin_pct,
    GREATEST(et.quoted_total - et.rev_inr, 0.00)::NUMERIC(12,2) AS balance_pending,
    et.project_type,
    et.pipeline_stage,
    et.material_stage,
    et.payment_stage,
    et.zoho_tag_option_id
FROM effective_totals et;

CREATE OR REPLACE VIEW public.v_whos_doing_what
WITH (security_invoker = true) AS
SELECT
    'project_pipeline'::TEXT AS work_kind,
    p.id AS project_id,
    NULL::UUID AS item_id,
    p.project_code,
    p.customer_name,
    p.site_area_landmark,
    p.pipeline_stage AS stage_code,
    pps.label AS stage_label,
    CASE
        WHEN p.pipeline_stage = 'estimating' AND cardinality(p.estimator_ids) > 0 THEN p.estimator_ids
        WHEN p.pipeline_stage = 'site_sizing_boq' AND p.sizing_mistri_id IS NOT NULL THEN ARRAY[p.sizing_mistri_id]
        ELSE p.current_owner_ids
    END AS assigned_person_ids,
    GREATEST(FLOOR(EXTRACT(EPOCH FROM (now() - p.pipeline_stage_at)) / 86400.0)::INTEGER, 0) AS days_in_stage,
    p.promised_delivery_date,
    p.pipeline_stage_at AS stage_entered_at
FROM public.projects p
JOIN public.project_pipeline_stages pps ON pps.code = p.pipeline_stage
WHERE p.project_type = 'customer'
  AND p.status = 'active'
  AND p.pipeline_stage <> 'execution'

UNION ALL

SELECT
    'material_track'::TEXT AS work_kind,
    p.id AS project_id,
    NULL::UUID AS item_id,
    p.project_code,
    p.customer_name,
    p.site_area_landmark,
    p.material_stage AS stage_code,
    mts.label AS stage_label,
    CASE
        WHEN p.material_owner_id IS NOT NULL THEN ARRAY[p.material_owner_id]
        ELSE '{}'::uuid[]
    END AS assigned_person_ids,
    GREATEST(FLOOR(EXTRACT(EPOCH FROM (now() - p.material_stage_at)) / 86400.0)::INTEGER, 0) AS days_in_stage,
    p.promised_delivery_date,
    p.material_stage_at AS stage_entered_at
FROM public.projects p
JOIN public.material_track_stages mts ON mts.code = p.material_stage
WHERE p.project_type = 'customer'
  AND p.status = 'active'
  AND p.material_stage IN ('check_stock', 'ordered_pending')

UNION ALL

SELECT
    'item_execution'::TEXT AS work_kind,
    p.id AS project_id,
    pi.id AS item_id,
    p.project_code,
    p.customer_name || ' — ' || pi.item_name AS customer_name,
    p.site_area_landmark,
    pi.stage AS stage_code,
    ws.label AS stage_label,
    CASE
        WHEN pi.stage = 'in_shop' AND pi.production_mistri_id IS NOT NULL THEN ARRAY[pi.production_mistri_id]
        WHEN pi.stage = 'at_site' AND pi.assembly_mistri_id IS NOT NULL THEN ARRAY[pi.assembly_mistri_id]
        ELSE '{}'::uuid[]
    END AS assigned_person_ids,
    GREATEST(FLOOR(EXTRACT(EPOCH FROM (now() - pi.stage_entered_at)) / 86400.0)::INTEGER, 0) AS days_in_stage,
    COALESCE(pi.target_date, p.promised_delivery_date) AS promised_delivery_date,
    pi.stage_entered_at
FROM public.project_items pi
JOIN public.projects p ON p.id = pi.project_id
JOIN public.workshop_stages ws ON ws.code = pi.stage
WHERE p.project_type = 'customer'
  AND p.status = 'active'
  AND pi.stage <> 'qc_done';

CREATE OR REPLACE VIEW public.v_needs_info_queue
WITH (security_invoker = true) AS
SELECT
    ag.id AS goal_id,
    ag.owner_person_id,
    pe.full_name AS owner_name,
    ag.source_channel,
    ag.domain_scope,
    ag.goal_type,
    ag.status,
    ag.summary_title,
    ag.heard_transcript,
    ag.pending_prompt,
    ag.missing_slots,
    ag.collected_slots,
    ag.repair_options,
    ag.project_id,
    ag.customer_id,
    ag.created_at,
    ag.updated_at
FROM public.agent_goals ag
LEFT JOIN public.people pe ON pe.id = ag.owner_person_id
WHERE ag.status IN ('parked_for_clerk', 'needs_error_fix', 'gathering_info', 'awaiting_confirmation')
ORDER BY
    CASE ag.status
        WHEN 'needs_error_fix' THEN 1
        WHEN 'parked_for_clerk' THEN 2
        WHEN 'awaiting_confirmation' THEN 3
        ELSE 4
    END,
    ag.updated_at DESC;

-- -----------------------------------------------------------------------------
-- 8. Row Level Security (RLS) on EVERY New Table
--    (TO authenticated + active app_users email check; UPDATE has USING + WITH CHECK)
-- -----------------------------------------------------------------------------
ALTER TABLE public.project_pipeline_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.material_track_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_track_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vendors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.people ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_webhook_dedup ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_inbox_buffer ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_goals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.worker_attendance ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_fnf_sheets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.worker_payroll_entries ENABLE ROW LEVEL SECURITY;

-- Note: telegram_webhook_dedup and telegram_inbox_buffer intentionally have NO
-- authenticated policies; they are accessed strictly server-side via SUPABASE_SECRET_KEY.

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'project_pipeline_stages',
        'material_track_stages',
        'payment_track_stages',
        'customers',
        'vendors',
        'people',
        'payment_accounts',
        'stage_events',
        'agent_goals',
        'worker_attendance',
        'monthly_fnf_sheets',
        'worker_payroll_entries'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_select', tbl);
        EXECUTE format($pol$
            CREATE POLICY %I ON public.%I
                FOR SELECT TO authenticated
                USING (
                    EXISTS (
                        SELECT 1 FROM public.app_users au
                        WHERE au.is_active = TRUE
                          AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
                    )
                )
        $pol$, tbl || '_select', tbl);

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_insert', tbl);
        EXECUTE format($pol$
            CREATE POLICY %I ON public.%I
                FOR INSERT TO authenticated
                WITH CHECK (
                    EXISTS (
                        SELECT 1 FROM public.app_users au
                        WHERE au.is_active = TRUE
                          AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
                    )
                )
        $pol$, tbl || '_insert', tbl);

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_update', tbl);
        EXECUTE format($pol$
            CREATE POLICY %I ON public.%I
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
                )
        $pol$, tbl || '_update', tbl);
    END LOOP;
END
$$;
