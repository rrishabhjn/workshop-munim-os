-- =============================================================================
-- Workshop Munim OS — Kanban Stages Management & Item Parts (002_kanban_parts_and_stages.sql)
-- Additive migration:
--   1. Allow authenticated allowlisted users to create/rename/reorder workshop_stages
--   2. Add item_parts table for optional lightweight sub-parts inside a furniture item
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Allow stage creation, renaming, and reordering via UI
-- -----------------------------------------------------------------------------
CREATE POLICY workshop_stages_insert ON public.workshop_stages
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY workshop_stages_update ON public.workshop_stages
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

-- -----------------------------------------------------------------------------
-- 2. Optional lightweight sub-parts per furniture item (e.g., Bed Box, Headboard)
-- -----------------------------------------------------------------------------
CREATE TABLE public.item_parts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    item_id UUID NOT NULL REFERENCES public.project_items(id) ON DELETE CASCADE,
    part_name TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'design_measurement' REFERENCES public.workshop_stages(code),
    assigned_karigar TEXT,
    is_done BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_item_parts_item_id ON public.item_parts (item_id);
CREATE INDEX idx_item_parts_project_id ON public.item_parts (project_id);
CREATE INDEX idx_item_parts_stage ON public.item_parts (stage);

CREATE TRIGGER trg_item_parts_updated_at
    BEFORE UPDATE ON public.item_parts
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.item_parts ENABLE ROW LEVEL SECURITY;

CREATE POLICY item_parts_select ON public.item_parts
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY item_parts_insert ON public.item_parts
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY item_parts_update ON public.item_parts
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

CREATE POLICY item_parts_delete ON public.item_parts
    FOR DELETE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );
