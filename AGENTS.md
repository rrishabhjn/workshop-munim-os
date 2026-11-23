# AGENTS.md — Workshop Munim OS

Read `GEMINI.md`, `docs/BLUEPRINT.md`, and `docs/PROVISIONING.md` before proposing architecture changes or writing code.

**Past conversation history (on `workshop-dev-box` only):** Earlier Antigravity conversations from the laptop are archived read-only at `~/agy-archive/`. Start with `~/agy-archive/INDEX.md`, then search `~/agy-archive/<id>/.system_generated/logs/transcript.jsonl` (and artifacts `~/agy-archive/<id>/*.md`) when the user references prior decisions or past work. Never copy archive contents into Git (transcripts may contain secrets).

## Working Conventions
- Build cleanly for a non-engineer small-business owner, inventory coordinator, and local data-entry clerk in India: prioritize low friction, mobile-first ergonomics (44px+ touch targets), clear Hinglish/English labels, and Indian rupee formatting (`₹1,15,000` with `en-IN` grouping).
- Keep all secrets out of Git; reference `.env.example` locally and GCP Secret Manager in Cloud Run.
- Keep migrations sequential under `supabase/migrations/` and verify every SQL view and RLS policy against the security checklist in `GEMINI.md`.
- Always verify webhook idempotency (`update_id` dedup in `telegram_webhook_dedup` / `telegram_state`), fast webhook acknowledgment (async Cloud Tasks for Gemini / Zoho API calls), explicit `ThinkingConfig` on `gemini-4-hydrogen` calls, and duplicate bill detection (`Supplier + Invoice_No + Amount`) before creating Zoho Bills.
- Enforce Risk-Tiered AI Autonomy: unambiguous Production lifecycle updates may auto-commit with a reversible `[Undo]` token (`stage_events`), whereas money/Zoho writes, new customers/entities, and ambiguous matches must pause for confirmation/disambiguation; upon confirmation, unpaid Supplier Bills (*Udhaar* across Munim and Inventory) post as `open` Accounts Payable and auto-clear FIFO on `vendor_payment`, while automated Customer Invoices in Zoho Books must strictly remain `draft` and never message external customers directly.
