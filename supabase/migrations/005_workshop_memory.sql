-- =============================================================================
-- Workshop Munim OS — Adaptive Workshop Memory (005_workshop_memory.sql)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.workshop_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'style',
    source TEXT NOT NULL DEFAULT 'auto_correction',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workshop_memory_active
    ON public.workshop_memory (is_active, created_at DESC);

DROP TRIGGER IF EXISTS trg_workshop_memory_updated_at ON public.workshop_memory;
CREATE TRIGGER trg_workshop_memory_updated_at
    BEFORE UPDATE ON public.workshop_memory
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.workshop_memory ENABLE ROW LEVEL SECURITY;

CREATE POLICY workshop_memory_select ON public.workshop_memory
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY workshop_memory_insert ON public.workshop_memory
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );

CREATE POLICY workshop_memory_update ON public.workshop_memory
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

CREATE POLICY workshop_memory_delete ON public.workshop_memory
    FOR DELETE TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.app_users au
            WHERE au.is_active = TRUE
              AND lower(au.email) = lower(coalesce(auth.jwt() ->> 'email', ''))
        )
    );
