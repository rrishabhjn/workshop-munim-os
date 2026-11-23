# Privacy & Data Handling

_Last updated: 23 November 2026_

Workshop Munim OS is an **internal** tool. It has no public sign-up, and it never messages customers or suppliers. This document explains what personal data it processes, where that data lives, and how long it is kept. It is written with India's **Digital Personal Data Protection Act, 2023 (DPDP Act)** in mind.

> [!NOTE]
> This is a good-faith description of how the system is built, not legal advice. The operating workshop should have it reviewed by its CA or a lawyer.

## 1. Roles

| Role | Who |
| :--- | :--- |
| **Data Fiduciary** (decides why and how data is used) | The operating custom furniture & interiors workshop |
| **Technology provider** (builds and maintains the system) | Rishabh Jain |
| **Grievance / privacy contact** | Rishabh Jain, via the contact listed in the staff notice or https://github.com/rrishabhjn |

## 2. Whose data, and what

| Data principal | Data processed | Purpose |
| :--- | :--- | :--- |
| **Office operators** (owner, purchase, estimators, clerk) | Name, Google email, Telegram user ID, messages and voice notes sent to the bot | Sign-in, allowlisting, running the AI assistant |
| **Salaried workers** | Name, Telegram user ID, **attendance photos** with server timestamps, worked/overtime hours, salary, advances, payout account aliases | Attendance, overtime and monthly payroll |
| **Mistris / contractors** | Name, phone, job assignments, piece-rate payables | Production tracking, job cards, vendor accounting |
| **Customers** | Name, area/landmark, phone, order details, payments | Order tracking and internal bookkeeping |
| **Suppliers** | Name, phone, bills (photos/PDFs), payables | Purchasing, inventory and bookkeeping |

The system does **not** collect Aadhaar, PAN, biometric templates, or location tracking. Attendance photos are stored as ordinary images; no face recognition is performed. Workers are matched by their Telegram account.

## 3. Where data lives (sub-processors)

| Service | What it holds | Region |
| :--- | :--- | :--- |
| Supabase (PostgreSQL + Auth) | Operational database, master directory, AI goal state | Mumbai (`ap-south-1`) |
| Google Cloud Storage | Compressed photos: attendance, bills, design/site photos | Mumbai (`asia-south1`) |
| Google Cloud Run / Tasks / Scheduler | Application compute (no persistent storage) | Mumbai (`asia-south1`) |
| Google Gemini API (paid tier) | Messages, voice notes and images, processed transiently to understand requests | Google-managed; may be processed outside India |
| Zoho Books India | Financial ledger: contacts, receipts, bills, expenses, payroll payouts | Zoho India data centre (`zoho.in`) |
| Telegram | Bot chats in the three internal groups | Telegram-managed; outside India |
| Google Sheets | Nightly read-only backup of key tables | Google-managed |

The Gemini API is used on a **paid** tier, under which Google's terms state that prompts and responses are not used to train its models.

## 4. Security controls

- Access is limited to an allowlist of office operators (Google Sign-In or HMAC-verified Telegram Mini App sessions). Workers and mistris have no login.
- Row Level Security is enabled on every database table. Server secrets live in GCP Secret Manager, never in the frontend or in Git.
- The media bucket is private and served only through short-lived signed URLs.
- All Telegram webhooks are verified with a secret token. The bot answers only allowlisted users, and the attendance group accepts only check-in/out photos from registered workers.
- Job cards shared with mistris contain **no prices**. No message is ever sent automatically to a customer or supplier.

## 5. Retention

| Data | Retention |
| :--- | :--- |
| Attendance photos | **90 days**, then deleted (hours and overtime are already recorded) |
| Attendance, payroll and financial records | **8 years**, in line with Indian books-of-account requirements |
| AI conversation state (`agent_goals`) and chat transcripts | Until the goal is completed or discarded, then up to 1 year for audit |
| Supplier bill and order photos | For the life of the related financial record (up to 8 years) |
| Former workers' contact details | Removed on request, except records that must be kept under law (payroll, accounts) |

> [!IMPORTANT]
> **Status:** The 90-day photo deletion and the conversation-state cleanup are policy commitments. Automated enforcement (a GCS lifecycle rule and a scheduled purge job) has not been implemented yet.

## 6. Your rights

Anyone whose data is in the system (staff, customers, suppliers) can ask the workshop to see it, correct it, or delete data that is no longer legally needed, and can withdraw consent to attendance photos and switch to a manual register. Contact the grievance contact above. Requests are answered within 30 days.

## 7. Breaches

If personal data is exposed, the workshop will notify affected people and, where required, the Data Protection Board of India without delay.

Staff-facing version (Hindi/English): [`docs/legal/STAFF_PRIVACY_NOTICE.md`](docs/legal/STAFF_PRIVACY_NOTICE.md)
