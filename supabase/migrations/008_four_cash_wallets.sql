-- ============================================================================
-- Migration 008: Configure 4 Cash Wallets (Ansh, Kamlesh, Rohit, Shreyansh),
--   deactivate unused floats (Prakash Ji / Manu Bhaiya), and add bare-name
--   shorthand aliases while preserving word-bounded cash_galla / bank routing.
-- ============================================================================

INSERT INTO public.people (
    full_name,
    person_type,
    role_tags,
    aliases,
    is_active
)
SELECT
    'Rohit',
    'operator',
    ARRAY['supervisor', 'float_custodian'],
    ARRAY['Rohit Ji', 'Rohit Bhaiya'],
    TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM public.people WHERE lower(full_name) = 'rohit'
);

UPDATE public.payment_accounts
SET is_active = FALSE
WHERE code IN ('float_prakash', 'float_manu');

INSERT INTO public.payment_accounts (
    code,
    display_name,
    account_kind,
    aliases,
    custodian_person_id,
    is_active,
    sort_order
) VALUES
    (
        'float_ansh',
        'Ansh Wallet',
        'supervisor_float',
        ARRAY['ansh', 'ansh wallet', 'ansh float', 'ansh cash', 'ansh ji', 'ansh bhaiya'],
        (SELECT id FROM public.people WHERE lower(full_name) = 'ansh' ORDER BY created_at ASC LIMIT 1),
        TRUE,
        10
    ),
    (
        'float_kamlesh',
        'Kamlesh Wallet',
        'supervisor_float',
        ARRAY['kamlesh', 'kamlesh wallet', 'kamlesh float', 'kamlesh cash', 'kamlesh bhaiya', 'kamlesh ji'],
        (SELECT id FROM public.people WHERE lower(full_name) IN ('kamlesh', 'kamlesh bhaiya') ORDER BY created_at ASC LIMIT 1),
        TRUE,
        11
    ),
    (
        'float_rohit',
        'Rohit Wallet',
        'supervisor_float',
        ARRAY['rohit', 'rohit wallet', 'rohit float', 'rohit cash', 'rohit ji', 'rohit bhaiya'],
        (SELECT id FROM public.people WHERE lower(full_name) = 'rohit' ORDER BY created_at ASC LIMIT 1),
        TRUE,
        12
    ),
    (
        'float_shreyansh',
        'Shreyansh Wallet',
        'supervisor_float',
        ARRAY['shreyansh', 'shreyansh wallet', 'shreyansh float', 'shreyansh cash', 'shreyansh ji', 'owner wallet'],
        (SELECT id FROM public.people WHERE lower(full_name) = 'shreyansh' ORDER BY created_at ASC LIMIT 1),
        TRUE,
        13
    )
ON CONFLICT (code) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    account_kind = EXCLUDED.account_kind,
    aliases = EXCLUDED.aliases,
    custodian_person_id = EXCLUDED.custodian_person_id,
    is_active = EXCLUDED.is_active,
    sort_order = EXCLUDED.sort_order;
