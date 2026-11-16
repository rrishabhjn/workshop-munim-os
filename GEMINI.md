# Workshop Munim OS — Project Rules & Unified Context (GEMINI.md)

## 1. Project Overview & Multi-Domain Vision
We are building **Workshop Munim OS**, an AI-native multi-domain operations, production lifecycle, attendance/payroll, and bookkeeping platform for a custom furniture & turnkey interior manufacturing workshop in Beawar, Rajasthan, India (executing standalone pieces + ₹3L–₹23L multi-city interior projects across Beawar, Jaipur, Ajmer, Chittorgarh, etc.).

All domains share **one canonical Supabase PostgreSQL master data spine** (`customers`, `vendors`, `people`, `payment_accounts`, `projects`), while staying cleanly separated across **2 web/Mini App domains** and **3 purpose-built Telegram channels**:

1. **Domain 1 — Production Lifecycle App (`/production`) + Production Telegram Channel:**
   - **Operated strictly by the ~4–5 Office/Supervisor Operators:** Owner (`Shreyansh`), Inventory/Purchase (`Ansh`), `Estimator(s)`, and `Data-Entry Clerk`. Full financial trust inside the logged-in office team.
   - **Mistris / Karigars never log into the web apps:** Tracked as named assignees (`people` / `vendors`), appearing in a **Mistri Floor & Site Load** view managed by Shreyansh/office staff, and receiving price-free **WhatsApp Job Card PNGs**.
2. **Domain 2 — Munim & HR App (`/munim`) + Munim Finance Telegram Channel + Worker Attendance Group:**
   - **Zoho Books India = 100% Internal Management Book of Record (GST-Off):** Records 100% of real money at gross deal value across all Banks/OD (`Axis`, `RGB`, `CCRGB`, `ICICI`, `IDFC`, `SBI`, `Canara`, `Paytm`), `Cash Galla`, Supervisor Floats (`Prakash Ji`, `Kamlesh Bhaiya`, `Manu Bhaiya`, `Ansh`), Customer Debtors, and Vendor Advances/Bills. Official GST tax invoices and GSTR filings are handled separately with the local CA.
   - **Worker Photo Attendance (`>6:00 PM` OT) & Monthly FnF Payroll:** ~18 salaried workers post arrival/departure photos from their own smartphones in the **Telegram Attendance Group** (matched via `telegram_user_id` + server `IST` timestamp), feeding a discretionary **Monthly FnF Settlement Sheet** with mid-month advance set-off and proxy payout aliases (`Ruksar Bano → Lala Ji`).
   - **Admin (Repo Owner):** Remote setup and light maintenance from the US.

## 2. Locked Tech Stack & Architecture Invariants
Do NOT suggest switching away from these decided components unless the user explicitly asks:

- **Internal Management Accounting Engine:** Zoho Books India (`zoho.in`, **Standard Plan ~₹749/mo**)
  - Always use Indian regional endpoints: `https://www.zohoapis.in/books/v3` and `https://accounts.zoho.in` (never `.com`).
  - Configured as a **GST-off (or tax-exempt default) Internal Management Ledger** tracking real gross deal values across all Banks, `Cash Galla`, and `Petty Cash` Supervisor Floats.
  - **Zoho Standard Plan Order Tagging (No Professional `Projects` Required):** Link customer orders `1:1` using **Zoho Reporting Tags** (`zoho_tag_option_id` under a `Project` Reporting Tag) + `cf_project_code` / `reference_number` rather than Zoho Professional's `Projects` module.
  - **Smart Auto-Bridge:** `Leads` live strictly in Supabase (`Production`) so dead enquiries never clutter Zoho. Zoho Contact + `1:1` Zoho Reporting Tag option auto-create on deal conversion (`Advance Received` / `Site Sizing / BOQ` or first financial entry), and customer receipts in Zoho auto-update Production's `Payment Track` pill.
  - **Instant Push, Idempotency & Draft Boundary:** Stamp deterministic `goal_step_id` into Zoho's `reference_number` (querying before retry so timeouts never double-post; auto-retry `429/5xx` via Cloud Tasks backoff and wake LLM repair only on `400` validation errors). Upon 1-tap `[✅ Confirm]`, Expenses, Float Transfers, Customer Receipts, Vendor Advances, and Salary Payouts push directly to Zoho Books; automated **Supplier Bills and Customer Invoices MUST strictly be created as `draft`** so the Clerk verifies line items/totals inside Zoho Books. Never finalize/send Zoho invoices automatically.
- **Operational Database & Auth:** Supabase (`ap-south-1` Mumbai)
  - PostgreSQL is the canonical operational & master-data source of truth.
  - Google Sign-In for desktop/shop PC browsers + server-side HMAC verification of Telegram `initData` for Mini Apps.
- **File & Media Storage:** Private Google Cloud Storage (GCS) bucket in `asia-south1` (`GCS_STORAGE_BUCKET`, Uniform Bucket-Level Access) storing compressed WebP photos (max 1600px, ~150KB each: design/sizing/pre-pack photos, supplier bills, worker attendance check-ins, Karigar Job Cards, and legacy archive), served strictly via short-lived V4 signed URLs or authenticated backend proxy.
- **Omnichannel AI-Native Engine (`AgentCore`) & Telegram Channels:**
  - Single channel-agnostic `AgentCore` powers **both** the 3 Telegram channels (`Production`, `Munim Finance`, `Worker Attendance`) **and** the persistent **Context-Aware Voice/Text Command Bar + `Needs Info` Drawer** inside the Web/Mini Apps.
  - Strictly internal (allowlisted `telegram_user_id`s / `app_users` emails; Attendance group scoped strictly to check-in/out photos for registered workers). Never messages external customers or suppliers directly; drafts copy-pasteable WhatsApp messages instead.
  - Verify `X-Telegram-Bot-Api-Secret-Token` on every webhook POST, deduplicate `update_id` in `telegram_webhook_dedup`, and buffer incoming bursts per `(chat_id, user_id)` in `telegram_inbox_buffer` with a **2.5s Cloud Tasks debounce + Postgres advisory lock** (plus atomic compare-and-swap on `[Confirm]` / `[Undo]` taps so rapid messages or 4G double-taps never race).
  - **Quiet Personal-Assistant Contract & Risk-Tiered Autonomy:**
    - Never send unsolicited reminders; never auto-expire open `agent_goals` (every goal ends in `completed` or explicit `[❌ Discard]`, surfacing quietly in the `Needs Info` badge and `08:30 IST` brief footer); consolidate missing slots into 1 question; always echo `🎙️ Heard: "..."` on voice inputs.
    - **Unambiguous Production Lifecycle Moves:** Auto-commit immediately with a 1-tap `[↩️ Undo]` button (backed by `stage_events` rollback).
    - **Money, Zoho Writes, New Entities & Ambiguities:** Always pause to disambiguate (`Existing Order` vs `Repeat Customer` vs `New Customer: Full Name + Site Area + Rough Scope`) or show a `₹` summary card (`en-IN` grouping) with `[✅ Confirm]` `[✏️ Edit]` `[❌ Cancel]`.
    - **Step-Checkpointed Self-Healing:** Canonical multi-day/cross-channel state lives in `agent_goals` (`collected_slots`, `missing_slots`, `steps[]`); use `previous_interaction_id` strictly within an active multi-step tool turn. Feed `400` validation errors into Gemini to propose 1-tap chat fixes + **`[📥 Let Clerk Fix Later]`** (routing to the `Needs Info` drawer).
- **Serverless Compute & Scheduling:** Google Cloud Run (`asia-south1` Mumbai), Cloud Tasks, Cloud Scheduler, and GCP Secret Manager.
  - Single Cloud Run container serves FastAPI + React/Tailwind PWA (`/production` and `/munim`). No always-on VM.
  - Scheduled jobs: `08:30 IST` Morning Brief (anti-spam bottleneck + delivery radar) and `02:00 IST` Zoho balance/Owner-Pulse cache sync + one-way read-only Google Sheets backup (also prevents Supabase Free Tier 7-day inactivity pause).
- **AI Models (`google-genai >= 3.2.0`):**
  - **Front-Door Triage & Routing (`v1beta` Decisions API):** `gemini-decision-1` (`client.decisions.create`, \$0.10/1M input, \$0 output) for group-chatter filtering, Multi-Goal topic routing, photo/PDF classification + OCR legibility rubric (`0..2`), attendance photo checks, and `workshop_memory` worthiness gating.
  - **Primary Agent & Multimodal Model (`v1` GA Interactions API):** `gemini-4-hydrogen` (`client.interactions.create` with `previous_interaction_id` stored on `agent_goals` + Context Caching + **explicit `ThinkingConfig`** so it never silently defaults to `high`):
    - `low` thinking: routine voice/text turns, tool calls, slot-filling, `workshop_memory` rule distillation.
    - `medium` thinking: multi-item compound dumps, printed supplier bills, and Zoho/Supabase error self-healing.
    - `high` thinking: reserved strictly for blurry/handwritten Beawar *kacchi parchis*.
  - **Historical Printed Batch Tier:** `gemini-3.5-flash-lite` (`low` thinking, 50%-off Batch API) strictly for clean computer-printed historical PDFs.
  - Never use legacy `gemini-2.5-*` / `gemini-2.0-*` / `google-generativeai`, skip `gemini-3.8-flash` (intro pricing ends Dec 31, 2026), and do not use frontier Carbon/Argon tiers at runtime.

## 3. Schema Design & Supabase Security Rules
- **Row-Extensible Lookups & Precision:**
  - Never use rigid Postgres `ENUM` types for lifecycle stages, tracks, or categories; use `TEXT` columns backed by lookup tables (`project_pipeline_stages`, `material_track_stages`, `payment_track_stages`, `workshop_stages`, `material_categories`).
  - Include `metadata JSONB NOT NULL DEFAULT '{}'` on core operational tables and use `NUMERIC(12,2)` for all monetary amounts (never floats).
  - Keep migrations sequential under `supabase/migrations/` (`001_init.sql` .. `006_...sql`).
- **Supabase Security Invariants:**
  - Enable Row Level Security (RLS) on every table in `public`.
  - Define SQL views with `WITH (security_invoker = true)`.
  - Use `TO authenticated` (never `auth.role()`) paired with an active `app_users` allowlist check (never rely on `TO authenticated` alone or `user_metadata`).
  - Every `UPDATE` policy must include both `USING` and `WITH CHECK`, plus a matching `SELECT` policy.
  - Never expose `SUPABASE_SECRET_KEY` (`service_role`) in frontend code; use `SUPABASE_PUBLISHABLE_KEY` in frontend and `SUPABASE_SECRET_KEY` strictly server-side.
  - Avoid `SECURITY DEFINER` functions in `public`. Pin dependency versions and commit lockfiles.

## 4. Domain Product Requirements (`v1` Locked, `v2` Parked)
### Domain 1: Production Lifecycle (`/production`)
1. **Project Pipeline (`Tier 1`):** `Lead` → `Design` → `Estimating` (1+ Estimators) → `Quoted to Customer / Awaiting Advance` → `Site Sizing / BOQ` (Sizing Mistri + Scope-Change toggle) → `Execution` → `Completed` *(+ `On Hold` / `Lost`)*.
2. **Parallel Project Tracks:**
   - **Material Track (Default: `Ansh`):** `Check Stock` → `Ordered (Pending)` → `All In Shop` (never hidden when shop cutting starts early).
   - **Payment Track (Synced from Zoho/Munim):** `Advance Pending` → `Advance In` → `Partial / Pre-Dispatch` → `Payment Complete`.
3. **Flat Item Execution (`Tier 2` — `'Kitchen'`, `'Master Bed'`, etc.):** `In Shop` (`production_mistri_id` preserved permanently) → `Packed / Ready` (manual pre-bubble-wrap shop check + pre-dispatch payment badge) → `At Site` (`assembly_mistri_id`) → `QC Done` (`Shreyansh / Head Karigar` pass-only milestone) + 1-tap price-free **Karigar Job Card PNG**.
4. **2 Core Screens:** `Who's Doing What` (`My Queue` + `Office Team Queues` + `Mistri Floor & Site Load` with `Days in Stage` aging badges) and `All Orders` (unified expandable board) + `Team & Mistri Roster` drawer.

### Domain 2: Munim & HR (`/munim`)
1. **Worker Attendance & Monthly FnF Payroll:** Self-phone Telegram photo check-ins/outs, automated hours + `>6:00 PM` OT calculation, month-tagged salary advances (`Oct Adv` / `Nov Adv`) + proxy bank aliases, discretionary Monthly FnF approval sheet, and payout push to Zoho.
2. **Mobile Owner Pulse:** Read-only live Zoho mirror (`Cash Galla`, `Banks`, `Supervisor Floats`, `Customer Debtors`, `Vendor Dues`, `Project P&L`).
3. **Shared Master Directory & Archive:** Canonical `Customers`, `Vendors` (suppliers + piece-rate contractors), `People`, `Payment Accounts`, aliases, and `historical_archive` search.

### Explicit Scope Boundaries
- **Permanently Excluded:** In-app pre-sales BOM / brand-tier quote pricing calculator (costing stays external; only deal value & lifecycle milestones enter the OS).
- **Parked for `v2`:** Structured BOQ material verification (`estimated_qty` vs `verified_boq_qty`), Ansh shop stock check (`v_master_purchase_list`), and Factory Gate-In / Site Material Challans + Returnable Tools.
