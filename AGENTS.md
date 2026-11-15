# AGENTS.md — Workshop Munim OS

Read `GEMINI.md`, `docs/BLUEPRINT.md`, and `docs/PROVISIONING.md` before proposing architecture changes or writing code.

## Working Conventions
- Build cleanly for a non-engineer small-business owner and local data-entry clerk in India: prioritize low friction, mobile-first ergonomics (44px+ touch targets), clear Hinglish/English labels, and Indian rupee formatting (`₹1,15,000` with `en-IN` grouping).
- Keep all secrets out of Git; reference `.env.example` locally and GCP Secret Manager in Cloud Run.
- Keep migrations strictly additive under `supabase/migrations/` and verify every SQL view and RLS policy against the security checklist in `GEMINI.md`.
- Always verify webhook idempotency (`update_id` dedup in `telegram_state`), fast webhook acknowledgment (async Cloud Tasks for Gemini OCR / Zoho API calls), and duplicate bill detection (`Supplier + Invoice_No + Amount`) before creating Zoho Draft Bills.
- Never allow automated code paths to finalize/send Zoho Books invoices or message external customers directly.
