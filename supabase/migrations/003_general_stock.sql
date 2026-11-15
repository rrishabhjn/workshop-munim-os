-- =============================================================================
-- Workshop Munim OS — General Stock Bucket for Multi-Order Bills (003_general_stock.sql)
-- =============================================================================

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS project_type TEXT NOT NULL DEFAULT 'customer'
    CHECK (project_type IN ('customer', 'general_stock'));

INSERT INTO public.projects (
    project_code,
    customer_name,
    project_type,
    status,
    gst_flag,
    quoted_total
)
VALUES (
    'GEN-STOCK',
    'General Workshop Stock',
    'general_stock',
    'active',
    TRUE,
    0.00
)
ON CONFLICT (project_code) DO NOTHING;

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
    GREATEST(p.quoted_total - (p.zoho_invoiced_cache + COALESCE(c.cash_in, 0.00)), 0.00)::NUMERIC(12,2) AS balance_pending,
    p.project_type
FROM public.projects p
LEFT JOIN cash_agg c ON c.project_id = p.id;
