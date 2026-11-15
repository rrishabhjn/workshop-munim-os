-- =============================================================================
-- Workshop Munim OS — Expand cash_ledger transaction types (004_expand_txn_types.sql)
-- =============================================================================

ALTER TABLE public.cash_ledger
    DROP CONSTRAINT IF EXISTS cash_ledger_txn_type_check;

ALTER TABLE public.cash_ledger
    ADD CONSTRAINT cash_ledger_txn_type_check
    CHECK (
        txn_type IN (
            'cash_advance',
            'cash_balance',
            'customer_receipt',
            'vendor_payment',
            'karigar_labor',
            'local_transport',
            'overhead_expense',
            'misc'
        )
    );
