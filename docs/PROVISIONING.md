# Account & Infrastructure Provisioning Checklist

## Account Ownership Rule (Avoid New-Gmail Anti-Abuse Bans)
Do NOT create a brand-new shared Gmail and immediately log into it from both the US and India with a billing card. Use existing aged personal accounts and grant access via native platform roles:

| Service | Own Under (Aged Account) | How the Other Person Accesses It |
| :--- | :--- | :--- |
| GCP Project + Gemini API | Your existing personal Gmail | Invite brother's existing Gmail via GCP IAM later |
| GitHub Repo | Your existing personal GitHub | Private repo; invite later if needed |
| Supabase Project | Your existing personal GitHub / Gmail | Invite brother's existing Gmail as Org Admin in Supabase |
| Zoho Books India | Brother's existing Gmail (India IP + GST match) | Adds your Gmail under Settings -> Users & Roles (Admin) or generates Self-Client refresh token |
| Backup Google Sheet | Brother's existing Gmail Drive | Shared (Editor) with your Gmail + GCP Service Account email |
| Telegram Bot (`@BotFather`) | Your existing Telegram (or brother's) | Allowlists numeric Telegram user IDs; independent of Gmail |

## Pre-Launch Business Checks (15-Minute Call with Brother & CA)
- Confirm local Beawar CA accepts Zoho Books India GSTR-1/GSTR-3B and purchase register exports.
- Confirm brother is comfortable logging informal cash/karigar payouts in cloud/Telegram (or decide whether to keep the tool strictly to formal bills + production/materials).

## Wave 1 — Provision Now to Build & Test Immediately (~15–20 mins)
1. **Telegram Bot (`@BotFather`):**
   - Run `/newbot`, name it `Workshop Munim` (or shop name), save `TELEGRAM_BOT_TOKEN`.
   - Message `@userinfobot` from your Telegram (and later brother's + data-entry staff's) to collect `ALLOWED_TELEGRAM_USER_IDS`.
2. **Supabase (`supabase.com`):**
   - Create Free Tier project in **South Asia (Mumbai) `ap-south-1`**.
   - Save DB password, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`, and `DATABASE_URL`.
3. **Google Cloud Project (`console.cloud.google.com`):**
   - Create personal project (e.g., `beawar-workshop-os`) with personal billing attached, target region `asia-south1` (Mumbai).
   - Enable APIs: `run.googleapis.com`, `cloudscheduler.googleapis.com`, `cloudtasks.googleapis.com`, `secretmanager.googleapis.com`, `sheets.googleapis.com`, `storage.googleapis.com`.
   - Create one Service Account (e.g., `workshop-os-runner`) for Cloud Run, Cloud Storage, & Scheduler.
   - Create a private **Google Cloud Storage** bucket in `asia-south1` (e.g., `workshop-munim-assets`) with Uniform Bucket-Level Access and grant `workshop-os-runner` `Storage Object Admin` (plus `Service Account Token Creator` if signing URLs via IAM).
4. **Google AI Studio (`aistudio.google.com`):**
   - Generate `GEMINI_API_KEY` linked to the personal GCP billing project (paid tier: unlocks `gemini-4-hydrogen`, `gemini-3.5-flash-lite`, Batch API, and avoids free-tier training data usage).
5. **Private GitHub Repository (`github.com`):**
   - Create private repo `workshop-munim-os` and place this starter bundle at its root.

## Wave 2 — Wire Zoho, Desktop Auth & Backups
6. **Zoho Books India (`zoho.in/books`):**
   - Sign up strictly on `zoho.in` (India DC), set state to Rajasthan (`08`), configure GSTIN, furniture HSN codes, and enable Projects.
   - At `api-console.zoho.in`, create a **Self Client** with scopes `ZohoBooks.bills.ALL,ZohoBooks.invoices.READ,ZohoBooks.contacts.ALL,ZohoBooks.projects.ALL`, and exchange grant code once for `ZOHO_REFRESH_TOKEN`.
7. **Google OAuth Client (Desktop/Shop PC Browser Sign-In):**
   - Create OAuth 2.0 Web Client ID in GCP Console and configure it under Supabase Auth -> Providers -> Google.
8. **Nightly Read-Only Backup Google Sheet:**
   - Create blank sheet `Workshop_Backup_ReadOnly`, share Editor access with the GCP Service Account email, and save `BACKUP_SHEET_ID`.
