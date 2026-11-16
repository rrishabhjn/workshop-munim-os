-- ============================================================================
-- Migration 007: Route packed_ready items to default QC owner (Shreyansh)
--   in v_whos_doing_what so My Queue highlights Shop Ready -> Pre-Bubble-Wrap QC
-- ============================================================================

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
        WHEN pi.stage = 'packed_ready' THEN COALESCE(
            NULLIF(
                ARRAY(
                    SELECT pe.id
                    FROM public.people pe
                    WHERE pe.is_active = TRUE
                      AND pe.is_default_qc = TRUE
                    ORDER BY pe.created_at ASC
                    LIMIT 1
                ),
                '{}'::uuid[]
            ),
            CASE WHEN pi.production_mistri_id IS NOT NULL THEN ARRAY[pi.production_mistri_id] ELSE '{}'::uuid[] END
        )
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
