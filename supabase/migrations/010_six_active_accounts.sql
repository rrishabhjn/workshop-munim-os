-- ============================================================================
-- Migration 010: Configure exact 6 active payment accounts
--   Banks/OD (3): Axis Bank, RGB, CCRGB
--   Cash & Wallets (3): Cash, Rohit Wallet, Kamlesh Wallet
--   Deactivate all other legacy banks/UPI/wallets.
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
WHERE code NOT IN (
    'cash_galla',
    'axis',
    'rgb',
    'ccrgb',
    'float_rohit',
    'float_kamlesh'
);

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
        'cash_galla',
        'Cash',
        'cash_galla',
        ARRAY['cash', 'cash galla', 'galla', 'shop cash', 'nakad'],
        NULL,
        TRUE,
        1
    ),
    (
        'axis',
        'Axis Bank',
        'bank',
        ARRAY['axis', 'axis bank'],
        NULL,
        TRUE,
        2
    ),
    (
        'rgb',
        'RGB',
        'bank',
        ARRAY['rgb', 'rajasthan gramin bank', 'gramin bank'],
        NULL,
        TRUE,
        3
    ),
    (
        'ccrgb',
        'CCRGB',
        'od_cc',
        ARRAY['ccrgb', 'cc rgb', 'od'],
        NULL,
        TRUE,
        4
    ),
    (
        'float_rohit',
        'Rohit Wallet',
        'supervisor_float',
        ARRAY['rohit', 'rohit wallet', 'rohit float', 'rohit cash', 'rohit ji', 'rohit bhaiya'],
        (SELECT id FROM public.people WHERE lower(full_name) = 'rohit' ORDER BY created_at ASC LIMIT 1),
        TRUE,
        5
    ),
    (
        'float_kamlesh',
        'Kamlesh Wallet',
        'supervisor_float',
        ARRAY['kamlesh', 'kamlesh wallet', 'kamlesh float', 'kamlesh cash', 'kamlesh bhaiya', 'kamlesh ji'],
        (SELECT id FROM public.people WHERE lower(full_name) IN ('kamlesh', 'kamlesh bhaiya') ORDER BY created_at ASC LIMIT 1),
        TRUE,
        6
    )
ON CONFLICT (code) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    account_kind = EXCLUDED.account_kind,
    aliases = EXCLUDED.aliases,
    custodian_person_id = EXCLUDED.custodian_person_id,
    is_active = EXCLUDED.is_active,
    sort_order = EXCLUDED.sort_order;
