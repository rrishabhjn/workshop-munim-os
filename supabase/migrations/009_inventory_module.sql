-- =============================================================================
-- Workshop Munim OS — Inventory & Procurement Module (009_inventory_module.sql)
-- Security & Architecture Invariants Enforced:
--   1. RLS enabled on EVERY new public table (inventory_items, inventory_movements)
--   2. Policies use TO authenticated + active app_users email check; UPDATE uses USING + WITH CHECK
--   3. Views defined WITH (security_invoker = true)
--   4. Zero rigid Postgres ENUMs; zero SECURITY DEFINER functions
--   5. NUMERIC(12,2) on all monetary & quantity columns + metadata JSONB NOT NULL DEFAULT '{}'
--   6. Clean & empty initial catalog (no pre-seeded dummy rows)
-- =============================================================================

-- 1. Standing Reusable Workshop Stock Catalog (Starts Clean & Empty)
CREATE TABLE IF NOT EXISTS public.inventory_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'ply_wood'
        REFERENCES public.material_categories(code),
    unit TEXT NOT NULL DEFAULT 'pcs',
    aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    on_hand_qty NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (on_hand_qty >= 0),
    min_stock_qty NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (min_stock_qty >= 0),
    avg_unit_cost_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (avg_unit_cost_inr >= 0),
    last_purchase_rate_inr NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (last_purchase_rate_inr >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inventory_items_name_trgm
    ON public.inventory_items USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_inventory_items_aliases_gin
    ON public.inventory_items USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_inventory_items_category_active
    ON public.inventory_items (category, is_active);

DROP TRIGGER IF EXISTS trg_inventory_items_updated_at ON public.inventory_items;
CREATE TRIGGER trg_inventory_items_updated_at
    BEFORE UPDATE ON public.inventory_items
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 2. Immutable Inventory & Gate-In Movement Ledger
CREATE TABLE IF NOT EXISTS public.inventory_movements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL DEFAULT gen_random_uuid(),
    inventory_item_id UUID REFERENCES public.inventory_items(id) ON DELETE RESTRICT,
    movement_type TEXT NOT NULL
        CHECK (movement_type IN (
            'stock_load',
            'direct_order_receipt',
            'customer_attribution',
            'count_adjust'
        )),
    qty_delta NUMERIC(12,2) NOT NULL,
    unit TEXT NOT NULL DEFAULT 'pcs',
    unit_rate_inr NUMERIC(12,2) CHECK (unit_rate_inr IS NULL OR unit_rate_inr >= 0),
    total_inr NUMERIC(12,2) CHECK (total_inr IS NULL OR total_inr >= 0),
    settlement_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (settlement_mode IN (
            'paid_now',
            'vendor_udhaar',
            'manual',
            'rate_pending',
            'attribution',
            'count_adjust'
        )),
    payment_account_id UUID REFERENCES public.payment_accounts(id) ON DELETE SET NULL,
    vendor_id UUID REFERENCES public.vendors(id) ON DELETE SET NULL,
    invoice_no TEXT,
    project_id UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    item_id UUID REFERENCES public.project_items(id) ON DELETE SET NULL,
    item_material_id UUID REFERENCES public.item_materials(id) ON DELETE SET NULL,
    material_label TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'ply_wood'
        REFERENCES public.material_categories(code),
    zoho_sync_status TEXT NOT NULL DEFAULT 'rate_pending'
        CHECK (zoho_sync_status IN (
            'rate_pending',
            'synced',
            'stubbed',
            'failed',
            'not_applicable'
        )),
    zoho_txn_type TEXT,
    zoho_txn_id TEXT,
    zoho_reference_number TEXT UNIQUE,
    actor_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    source_channel TEXT NOT NULL DEFAULT 'web'
        CHECK (source_channel IN ('web', 'telegram', 'system', 'undo')),
    notes TEXT,
    is_undone BOOLEAN NOT NULL DEFAULT FALSE,
    undone_at TIMESTAMPTZ,
    undone_by_person_id UUID REFERENCES public.people(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inv_movements_created
    ON public.inventory_movements (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_movements_batch
    ON public.inventory_movements (batch_id);
CREATE INDEX IF NOT EXISTS idx_inv_movements_sku
    ON public.inventory_movements (inventory_item_id, created_at DESC)
    WHERE inventory_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inv_movements_project
    ON public.inventory_movements (project_id, created_at DESC)
    WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inv_movements_rate_pending
    ON public.inventory_movements (zoho_sync_status, created_at DESC)
    WHERE zoho_sync_status = 'rate_pending' AND is_undone = FALSE;

DROP TRIGGER IF EXISTS trg_inventory_movements_updated_at ON public.inventory_movements;
CREATE TRIGGER trg_inventory_movements_updated_at
    BEFORE UPDATE ON public.inventory_movements
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3. Order-Level Materials Support (item_id Optional) + Inventory SKU Link
ALTER TABLE public.item_materials
    ALTER COLUMN item_id DROP NOT NULL;

ALTER TABLE public.item_materials
    ADD COLUMN IF NOT EXISTS inventory_item_id UUID REFERENCES public.inventory_items(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_item_materials_inv_item
    ON public.item_materials (inventory_item_id)
    WHERE inventory_item_id IS NOT NULL;

-- 4. Expand agent_goals CHECK Constraints for Inventory Domain & Actions
DO $$
DECLARE
    con RECORD;
BEGIN
    FOR con IN
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'public.agent_goals'::regclass
          AND contype = 'c'
    LOOP
        IF con.def ILIKE '%domain_scope%' OR con.def ILIKE '%goal_type%' THEN
            EXECUTE format('ALTER TABLE public.agent_goals DROP CONSTRAINT IF EXISTS %I', con.conname);
        END IF;
    END LOOP;
END
$$;

ALTER TABLE public.agent_goals
    ADD CONSTRAINT agent_goals_domain_scope_check
    CHECK (domain_scope IN ('production', 'inventory', 'munim', 'attendance', 'shared'));

ALTER TABLE public.agent_goals
    ADD CONSTRAINT agent_goals_goal_type_check
    CHECK (goal_type IN (
        'new_lead',
        'stage_update',
        'assign_owner',
        'financial_txn',
        'supplier_bill',
        'float_transfer',
        'salary_advance',
        'entity_onboarding',
        'inventory_load',
        'inventory_attribution',
        'inventory_adjust',
        'query',
        'repair'
    ));

-- 5. Recreate v_master_purchase_list (LEFT JOIN project_items so Order-Wide rows stay visible)
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
            'inventory_item_id', im.inventory_item_id,
            'project_id', p.id,
            'project_code', p.project_code,
            'customer_name', p.customer_name,
            'item_id', pi.id,
            'item_name', COALESCE(pi.item_name, 'Order-Wide'),
            'quantity', im.quantity,
            'status', im.status,
            'preferred_supplier', im.preferred_supplier,
            'notes', im.notes
        )
        ORDER BY p.promised_delivery_date NULLS LAST, p.customer_name, COALESCE(pi.item_name, '')
    ) AS customer_breakdown
FROM public.item_materials im
JOIN public.material_categories mc ON mc.code = im.category
JOIN public.projects p ON p.id = im.project_id
LEFT JOIN public.project_items pi ON pi.id = im.item_id
WHERE im.status IN ('needed', 'ordered')
  AND p.status = 'active'
GROUP BY im.category, mc.label, mc.sort_order, im.material_name, im.unit
ORDER BY mc.sort_order, im.material_name;

-- 6. Row Level Security (RLS) on All New Tables
ALTER TABLE public.inventory_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory_movements ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['inventory_items', 'inventory_movements']
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
