# Kickoff Prompt for Antigravity

Paste this prompt as your first message after opening the `workshop-munim-os` folder in Antigravity on your personal Google account:

---

Read `GEMINI.md`, `AGENTS.md`, `docs/BLUEPRINT.md`, `docs/PROVISIONING.md`, and `.env.example` completely.

All architecture, data model, and tooling decisions are already finalized — do not re-interview me on the stack or suggest switching to WhatsApp Cloud API, Firestore, an always-on VM, or legacy Gemini 2.5 models.

We are ready to begin building **Workshop Munim OS** from scratch in this repository. Start by creating the complete initial build scaffold in clean, verifiable steps:

1. Write `supabase/migrations/001_init.sql` implementing all tables (`workshop_stages`, `material_categories`, `app_users`, `projects`, `project_items`, `item_materials`, `cash_ledger`, `followups`, `entity_dictionary`, `historical_archive`, `telegram_state`), `metadata JSONB` extension columns, `security_invoker = true` SQL views (`v_master_purchase_list`, `v_project_pnl`), indexes, seed lookup rows, and strict RLS policies following `GEMINI.md`.
2. Scaffold the Python FastAPI backend (`backend/`) with:
   - Telegram webhook verification (`X-Telegram-Bot-Api-Secret-Token`, `ALLOWED_TELEGRAM_USER_IDS`, `update_id` deduplication, `media_group_id` photo handling, and inline `Confirm All` / `Edit` / `Cancel` callbacks).
   - Gemini multimodal router (`gemini-4-hydrogen` for Hinglish voice notes, handwritten bill OCR, and live Q&A; `gemini-3.5-flash-lite` for routine/batch tasks) grounded with `entity_dictionary` and active projects/items.
   - Zoho Books India (`zohoapis.in`) Draft Bill + nightly financial cache client (gracefully stubbed/feature-flagged until Wave 2 Zoho credentials are added).
   - Dashboard REST API endpoints + `08:30 IST` Morning Brief and `02:00 IST` Zoho/Sheets sync endpoints.
3. Scaffold the mobile-first React + Tailwind Telegram Mini App & Web Dashboard (`frontend/`) with Telegram `initData` auth + Supabase Google Sign-In, Customer Projects Board, Customer Detail Workspace, 6-Stage Floor Kanban, Master Purchase List, 1-click price-free Karigar Job Card PNG generator (Web Share API), Cash Ledger, Follow-Ups, and Historical Archive search.
4. Add a `Dockerfile` for single-container Cloud Run deployment (`asia-south1`), a `scripts/batch_archive_ocr.py` script for pre-cutoff historical bills, and a concise `README.md` with local run and deploy commands.

Start with Step 1 (`supabase/migrations/001_init.sql`) and ask me if I want to inspect it before moving to Steps 2–4, or proceed through the scaffold cleanly.
