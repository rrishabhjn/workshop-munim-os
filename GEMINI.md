# Workshop Munim OS — Project Rules & Context (GEMINI.md)

## 1. Project Overview
We are building **Workshop Munim OS**, a unified operations, production tracking, and bookkeeping companion for a custom made-to-order furniture manufacturing workshop in Beawar, Rajasthan, India.

- **Primary users:**
  - **Owner (Brother):** Phone-only on the workshop floor during the day, decently tech-savvy, also has a personal laptop and shop PC. Communicates in Hinglish/Hindi voice notes with Rajasthani/local names and furniture terminology (karigar, sunmica, udhaar, ply, baki).
  - **Data-Entry Person:** Works 30–45 minutes daily on the shop PC verifying formal GST bills in Zoho Books, reviewing cash logs, and keeping customer/supplier aliases clean.
  - **Admin (Repo Owner):** Remote setup and light maintenance from the US.
- **Core requirement:** Handle multi-item custom builds (e.g., Customer A -> Teak Bed, 6-Door Almirah, Study Table -> per-item stages, reference photos, and required materials) alongside both formal GST accounting and separate informal/cash operational tracking (karigar payouts, local transport, non-GST advances) without polluting GST books.

## 2. Locked Tech Stack & Architecture Invariants
Do NOT suggest switching away from these decided components unless the user explicitly asks:

- **Formal GST System of Record:** Zoho Books India
  - Always use Indian regional endpoints: `https://www.zohoapis.in/books/v3` and `https://accounts.zoho.in` (never `.com`).
  - Use Zoho Projects 1:1 per formal customer project.
  - **Draft-Only Boundary:** Automated bot actions must ONLY create supplier bills or customer invoices in `draft` status so the data-entry person verifies GST rates, HSN codes, and totals before approval.
- **Operational Database & Auth:** Supabase (Region: `ap-south-1` Mumbai)
  - PostgreSQL is the single operational source of truth.
  - Supabase Auth handles Google Sign-In for desktop/shop PC browsers; Telegram Mini App authenticates via server-side HMAC verification of Telegram `initData`.
- **File & Media Storage:** Private Google Cloud Storage (GCS) bucket in `asia-south1` (`GCS_STORAGE_BUCKET`, Uniform Bucket-Level Access) stores compressed WebP photos (max 1600px, ~150KB each), supplier bills, Karigar Job Card PNGs, and historical archives, served via short-lived V4 signed URLs or authenticated backend proxy.
- **Internal Bot & Dashboard Channel:** Telegram Bot API (`Workshop Munim` via `@BotFather`) + Embedded Telegram Mini App
  - Strictly internal (allowlisted numeric `telegram_user_id`s only). Never messages customers or suppliers directly; drafts copy-pasteable WhatsApp messages for the owner instead.
  - Verify `X-Telegram-Bot-Api-Secret-Token` on every webhook POST.
  - Use Telegram's native `media_group_id` for multi-photo bursts.
  - All operational/cash/material writes from voice notes require 1-tap inline confirmation (`Confirm All` / `Edit` / `Cancel`) before committing to PostgreSQL.
- **Serverless Compute & Scheduling:** Google Cloud Run (`asia-south1` Mumbai), Cloud Tasks (async webhook worker), Cloud Scheduler, and GCP Secret Manager.
  - Single Cloud Run container serves both the FastAPI backend (Telegram webhook + REST API + cron endpoints) and the React + Tailwind PWA / Telegram Mini App frontend.
  - No always-on VM.
  - Scheduled jobs: `08:30 IST` Daily Morning Brief (Telegram message + Mini App button) and `02:00 IST` Zoho financial cache refresh + one-way read-only backup mirror to Google Sheets (also keeps Supabase Free Tier active so it never hits the 7-day inactivity pause).
- **AI Models (Public Gemini API):**
  - **Primary live model:** `gemini-4-hydrogen` (`low` thinking for quick voice capture; `medium` for messy handwritten bill OCR and multi-step Q&A). Alternative during intro pricing through Dec 31, 2026: `gemini-3.8-flash`.
  - **Routine & historical batch tier:** `gemini-3.5-flash-lite` (standard or 50%-off Batch API) for simple lookups and one-time historical printed bill OCR.
  - Do NOT use legacy `gemini-2.5-flash` and do NOT use expensive frontier reasoning tiers (like Argon).
  - Always inject `entity_dictionary` + active `projects` and `project_items` names into Gemini prompts for Hinglish/Rajasthani name grounding, requesting strict JSON schema output.

## 3. Schema Design & Supabase Security Rules
- **Keep schema easy to evolve:**
  - Never use rigid Postgres `ENUM` types for workshop stages or material categories. Use `TEXT` columns backed by lookup tables (`workshop_stages`, `material_categories`) so new stages or categories are row inserts.
  - Include `metadata JSONB NOT NULL DEFAULT '{}'` on `projects`, `project_items`, `item_materials`, and `cash_ledger`.
  - Use `NUMERIC(12,2)` for all monetary amounts (never floats).
  - Keep all DDL changes as additive sequential migrations under `supabase/migrations/` (`001_init.sql`, `002_...sql`).
- **Supabase security invariants:**
  - Enable Row Level Security (RLS) on every table in `public`.
  - Define SQL views (`v_master_purchase_list`, `v_project_pnl`) with `WITH (security_invoker = true)`.
  - Use `TO authenticated` (never deprecated `auth.role()`) paired with an allowlist table check (`app_users`) — never rely on `TO authenticated` alone or user-editable `user_metadata`.
  - Every `UPDATE` policy must include both `USING` and `WITH CHECK`, plus a matching `SELECT` policy.
  - Never expose `SUPABASE_SECRET_KEY` (`service_role`) in frontend/client code; use `SUPABASE_PUBLISHABLE_KEY` in the frontend and `SUPABASE_SECRET_KEY` strictly server-side in Cloud Run.
  - Avoid `SECURITY DEFINER` functions in `public`.
  - Pin dependency versions and commit lockfiles (`package-lock.json`, `uv.lock` / `requirements.txt`).

## 4. Dashboard Product Requirements
1. **Customer Projects Board (Default):** One card per active customer with promised delivery date, combined formal + cash payment progress, and per-item stage pills; opens into the Customer Workspace (all items, inline photo galleries, dimensions, karigar, stage selector, per-item material checklist, follow-ups, SQL P&L).
2. **Floor Stage Kanban (1-Tap Toggle):** 6 default stages — `Design & Measurement` -> `Material Pending` -> `Carpentry` -> `Polish / Upholstery` -> `Ready` -> `Delivered`.
3. **Master Purchase List:** Backed by `v_master_purchase_list`, aggregating all `needed` materials across active customers by category with expandable per-customer breakdowns and status taps (`needed` -> `ordered` -> `in_shop`).
4. **1-Click "Share Karigar Job Card":** Generates a clean vertical PNG/PDF per furniture piece showing Customer Short Code + Item Name, Reference Photos, Dimensions & Specs, Assigned Karigar, Target Date, and Required Materials — strictly omitting all quotes, invoices, and payment balances — ready to share to WhatsApp via Web Share API.
5. **Back-Office Views:** Cash & Karigar Ledger (running cash balance, `verified_by_staff` checkboxes, 1-click CSV export), Follow-Ups, Entity Dictionary, and Historical Archive search.

## 5. Explicitly Parked / Rejected Approaches
- **Parked:** Passive WhatsApp scraping / ambient second brain / unofficial Baileys VM bridge (ban risk on primary business number) and Official WhatsApp Cloud API (replaced with Telegram to eliminate spare SIM, Meta Business Verification, and template overhead).
- **Rejected:** Firestore (replaced by Supabase Postgres), bidirectional live Google Sheets sync (replaced by nightly one-way read-only backup), always-on VM, and creating a shared brand-new Gmail login across US + India (use existing aged personal Gmails + platform role invites).
- **Rollout preference:** Build and shadow-test the complete all-in-one system upfront before handing over to the brother, using a clean cutoff date in Zoho Books + one-time Gemini Batch OCR archive of legacy bills.
