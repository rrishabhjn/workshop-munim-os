# Workshop Munim OS — Clean-Slate Reset & Go-Live Cutover Guide

This guide gives you a fast, zero-manual-entry cutover flow:
- **Kept intact:** Customers directory (`38`), Vendors & Piece-Rate Contractors (`53`, starting at `₹0` before your opening *Udhaar* load), Office Operators + Salaried Staff/Karigars (with Bank Proxy Aliases), and active Production Orders/Items.
- **Wiped / Reset:** All Zoho Books transactions, Supabase HR/attendance/payroll logs (`worker_attendance`, `worker_payroll_entries`, `monthly_fnf_sheets`), `cash_ledger`, `agent_goals` (`Needs Info`), Telegram dedup/buffers, `inventory_movements`, `inventory_items` qty/rates (`0`), and `contractor_mistri` rows removed from `people` (contractors remain in `vendors`).
- **6 Active Payment Accounts:**
  - **Banks / OD (3):** `Axis Bank`, `RGB`, `CCRGB`
  - **Cash & Wallets (3):** `Cash` (`Cash Galla`), `Rohit Wallet`, `Kamlesh Wallet`

---

## Step 1 — Wipe to Clean Slate

1. **Dry-Run Preview (read-only, zero deletions):**
   ```powershell
   .\backend\.venv\Scripts\python.exe scripts\reset_clean_slate.py --dry-run
   ```
2. **Execute Live Wipe (wipes Zoho transactions + opening balances, resets Supabase logs/caches & activates the 6 accounts):**
   ```powershell
   .\backend\.venv\Scripts\python.exe scripts\reset_clean_slate.py --execute --zero-zoho-opening
   ```
   *(If you apply SQL migrations manually in Supabase SQL Editor, `supabase/migrations/010_six_active_accounts.sql` mirrors the exact same 6-account setup.)*

---

## Step 2 — Send Raw Numbers → Script/API One-Time Load

Instead of manually clicking through Zoho Bills and every order screen, just paste your raw numbers in chat (or put them into a JSON file matching `scripts/cutover_opening_data.example.json`), and we run `scripts/load_cutover_opening.py` once to load all three datasets cleanly **without touching your 6 Bank/Cash balances**:

### Copy-Paste Chat Template

> **1. Active Customer Orders (`Order / Customer — Deal Value ₹ — Paid Till Date ₹`)**
> - `Ambure Ji (Jaipur Home)` — Deal: `₹23,00,000` — Paid Till Date: `₹15,00,000`
> - `Aakash Prajapati` — Deal: `₹10,21,100` — Paid Till Date: `₹9,00,000`
> - *(list only orders whose Deal Value or Paid Till Date needs updating)*
>
> **2. Supplier / Contractor Opening Udhaar (`Vendor Name — Amount We Owe ₹`)**
> - `R P Wood` — `₹85,000`
> - `Abhinandan Associates` — `₹42,000`
> - `Manish Cushioning` — `₹18,500`
> - *(posted automatically in Zoho as `Open` AP Bills against `Pre-Cutover Offset`, so future `vendor_payment` entries settle them oldest-first FIFO without touching Bank/Cash)*
>
> **3. Salaried Staff Base Salaries & Advances Already Paid This Month (`Worker Name — Monthly Salary ₹ — Advance Paid This Month ₹`)**
> - `Arvind Ji` — Salary: `₹39,000` — Nov Advance Paid: `₹10,000`
> - `Lala Ji` — Salary: `₹25,000` — Nov Advance Paid: `₹5,000` *(Proxy Alias: Ruksar Bano)*
> - *(updates `people.monthly_salary_inr`, saves `worker_payroll_entries`, and posts an expense against `Pre-Cutover Offset` / `Pre-Cutover Clearing` with `"Worker Name — ..."` so **`📒 Munim → Ledger / Passbook`** search surfaces them by worker name without touching Bank/Cash)*

### Running the One-Time Loader Script
```powershell
# Preview first (zero mutations)
.\backend\.venv\Scripts\python.exe scripts\load_cutover_opening.py --input cutover.json --dry-run

# Apply to Supabase + Zoho Books
.\backend\.venv\Scripts\python.exe scripts\load_cutover_opening.py --input cutover.json --execute
```

*(Optional anytime after cutover: to load existing ply/laminates/hardware sitting in the godown, go to **`📦 Inventory` → `Load Stock / Gate-In` → `🏗️ Opening / Manual`**, or send a raw stock list for loading.)*

*(Optional for Worker Photo Attendance: add each active worker's numeric `telegram_user_id` on their `people` row so photo check-ins/outs auto-match in the Attendance group.)*

---

## Step 3 (Final Step) — Enter the 6 Bank & Cash Opening Balances in Zoho Books + Sync

Once Steps 1–2 are done, log into **Zoho Books India (`books.zoho.in`) → Banking** (or **Settings → Opening Balances**) as your final step:

1. **Clean Slate Starting Point:** All old test opening balances in Zoho Books (`₹0.00` across every Bank/Cash/Contact account) are already cleared by Step 1. *(Optionally archive/deactivate any unused Zoho accounts outside these 6 in Banking.)*
2. **Enter Opening Balances for the 6 Active Accounts:**
   - `Axis Bank` *(Bank)* — real closing bank balance
   - `RGB` *(Bank)* — real closing bank balance
   - `CCRGB` *(Bank / Cash Credit OD)* — enter as **Credit / Negative** if overdrawn (e.g., `-4,50,000`), or positive if in credit
   - `Cash` (`Cash Galla` / `Cash`) — physical cash counted in shop *galla*
   - `Rohit Wallet` *(Cash)* — physical cash in Rohit's hand *(or negative/Credit if reimbursable out-of-pocket spend is owed)*
   - `Kamlesh Wallet` *(Cash)* — physical cash in Kamlesh's hand *(or negative/Credit if owed)*

> ### Critical As-Of Date Rule (`T0`)
> Zoho calculates live balances using:
> $$\text{Live Balance} = \text{Opening Balance (as of } T_0\text{)} + \text{Inflows on/after } T_0 - \text{Outflows on/after } T_0$$
> - **If you enter Opening Balances *before* logging any post-cutover transactions in the app:** set the Opening Date to today (`T0`) and enter your exact real balance right now.
> - **If you *already* logged new post-cutover transactions in the app on/after `T0`:** set the Opening Date to **before those new entries** (e.g., end-of-day yesterday or `T0`) and enter the balance **as of just before those entries** — otherwise Zoho will add those new transactions on top of a balance that already includes them.

3. **Final Smoke Check:**
   - Open **`📒 Munim` → `Sync Zoho Balances`** and confirm all 6 account cards on **Owner Pulse (Zoho Mirror)** match your exact numbers.
   - Open **`📒 Munim` → `Ledger / Passbook`**, search one worker with a pre-cutover advance to verify it appears under **`All Accounts`**, and confirm **`Needs Info`** shows `0`.
