import { useEffect, useState } from "react";
import {
  Archive,
  Building2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Wallet,
  X,
} from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";

function csvToArray(val) {
  return (val || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function MunimPortal({ munimData, onRefresh }) {
  const [subTab, setSubTab] = useState("pulse");
  const [syncing, setSyncing] = useState(false);

  // Quick-add mode ("vendor" | "customer")
  const [addMode, setAddMode] = useState("vendor");
  const [vendorName, setVendorName] = useState("");
  const [vendorKind, setVendorKind] = useState("supplier");
  const [vendorSpec, setVendorSpec] = useState("");
  const [vendorPhone, setVendorPhone] = useState("");
  const [vendorGstin, setVendorGstin] = useState("");

  const [custName, setCustName] = useState("");
  const [custArea, setCustArea] = useState("");
  const [custPhone, setCustPhone] = useState("");

  const [dirSearch, setDirSearch] = useState("");
  const [editingEntity, setEditingEntity] = useState(null); // { kind: 'customer'|'vendor'|'person', data: {...} }
  const [savingEdit, setSavingEdit] = useState(false);
  const [confirmArchive, setConfirmArchive] = useState(false);

  const [archiveQuery, setArchiveQuery] = useState("");
  const [archiveRows, setArchiveRows] = useState([]);

  const [ledgerRows, setLedgerRows] = useState([]);
  const [ledgerLoading, setLedgerLoading] = useState(false);
  const [ledgerError, setLedgerError] = useState("");
  const [ledgerSearch, setLedgerSearch] = useState("");
  const [ledgerAppliedSearch, setLedgerAppliedSearch] = useState("");
  const [ledgerAccount, setLedgerAccount] = useState("");
  const [ledgerType, setLedgerType] = useState("");

  async function loadLedger({
    account = ledgerAccount,
    q = ledgerAppliedSearch,
    entryType = ledgerType,
  } = {}) {
    setLedgerLoading(true);
    setLedgerError("");
    try {
      const qs = new URLSearchParams({ limit: "80" });
      if (account) qs.set("account", account);
      if (q && q.trim()) qs.set("q", q.trim());
      if (entryType) qs.set("entry_type", entryType);
      const res = await apiFetch(`/api/munim/ledger?${qs.toString()}`);
      setLedgerRows(res?.transactions || []);
    } catch (err) {
      setLedgerError(err?.message || "Failed to load ledger");
    } finally {
      setLedgerLoading(false);
    }
  }

  useEffect(() => {
    if (subTab === "ledger") {
      loadLedger({
        account: ledgerAccount,
        q: ledgerAppliedSearch,
        entryType: ledgerType,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab, ledgerAccount, ledgerType, ledgerAppliedSearch]);

  function openLedgerForAccount(accDisplayName) {
    setLedgerAccount(accDisplayName || "");
    setLedgerType("");
    setLedgerSearch("");
    setLedgerAppliedSearch("");
    setSubTab("ledger");
  }

  const accounts = munimData?.payment_accounts || [];
  const pnlRows = munimData?.project_pnl || [];
  const customers = munimData?.customers || [];
  const vendors = munimData?.vendors || [];
  const people = munimData?.people || [];

  const cashGalla = accounts.filter((a) => a.account_kind === "cash_galla");
  const floats = accounts.filter((a) => a.account_kind === "supervisor_float");
  const banks = accounts.filter(
    (a) => !["cash_galla", "supervisor_float"].includes(a.account_kind)
  );

  const qLow = dirSearch.trim().toLowerCase();
  const filteredCustomers = customers.filter(
    (c) =>
      !qLow ||
      [c.full_name, c.area_landmark, c.phone, ...(c.aliases || [])]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(qLow)
  );
  const filteredVendors = vendors.filter(
    (v) =>
      !qLow ||
      [v.canonical_name, v.specialty, v.phone, v.gstin, ...(v.aliases || [])]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(qLow)
  );
  const filteredPeople = people.filter(
    (p) =>
      !qLow ||
      [
        p.full_name,
        p.phone,
        ...(p.aliases || []),
        ...(p.payout_aliases || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(qLow)
  );

  async function handleSyncZoho() {
    setSyncing(true);
    try {
      await apiFetch("/api/munim/sync-zoho", { method: "POST" });
      await onRefresh();
    } finally {
      setSyncing(false);
    }
  }

  async function handleAddVendor(e) {
    e.preventDefault();
    if (!vendorName.trim()) return;
    await apiFetch("/api/munim/vendors", {
      method: "POST",
      body: JSON.stringify({
        canonical_name: vendorName.trim(),
        vendor_kind: vendorKind,
        specialty: vendorSpec.trim() || null,
        phone: vendorPhone.trim() || null,
        gstin: vendorGstin.trim() || null,
      }),
    });
    setVendorName("");
    setVendorSpec("");
    setVendorPhone("");
    setVendorGstin("");
    await onRefresh();
  }

  async function handleAddCustomer(e) {
    e.preventDefault();
    if (!custName.trim()) return;
    await apiFetch("/api/munim/customers", {
      method: "POST",
      body: JSON.stringify({
        full_name: custName.trim(),
        area_landmark: custArea.trim() || null,
        phone: custPhone.trim() || null,
      }),
    });
    setCustName("");
    setCustArea("");
    setCustPhone("");
    await onRefresh();
  }

  function openEditModal(kind, row) {
    setConfirmArchive(false);
    if (kind === "customer") {
      setEditingEntity({
        kind,
        id: row.id,
        full_name: row.full_name || "",
        area_landmark: row.area_landmark || "",
        phone: row.phone || "",
        aliases_csv: (row.aliases || []).join(", "),
        notes: row.notes || "",
      });
    } else if (kind === "vendor") {
      setEditingEntity({
        kind,
        id: row.id,
        canonical_name: row.canonical_name || "",
        vendor_kind: row.vendor_kind || "supplier",
        specialty: row.specialty || "",
        phone: row.phone || "",
        gstin: row.gstin || "",
        aliases_csv: (row.aliases || []).join(", "),
        notes: row.notes || "",
      });
    } else {
      setEditingEntity({
        kind: "person",
        id: row.id,
        full_name: row.full_name || "",
        person_type: row.person_type || "salaried_karigar",
        role_tags_csv: (row.role_tags || []).join(", "),
        phone: row.phone || "",
        aliases_csv: (row.aliases || []).join(", "),
        payout_aliases_csv: (row.payout_aliases || []).join(", "),
      });
    }
  }

  async function handleSaveEntityEdit(e) {
    e.preventDefault();
    if (!editingEntity) return;
    setSavingEdit(true);
    try {
      const { kind, id } = editingEntity;
      if (kind === "customer") {
        await apiFetch(`/api/munim/customers/${id}`, {
          method: "PATCH",
          body: JSON.stringify({
            full_name: editingEntity.full_name.trim(),
            area_landmark: editingEntity.area_landmark.trim(),
            phone: editingEntity.phone.trim(),
            aliases: csvToArray(editingEntity.aliases_csv),
            notes: editingEntity.notes.trim(),
          }),
        });
      } else if (kind === "vendor") {
        await apiFetch(`/api/munim/vendors/${id}`, {
          method: "PATCH",
          body: JSON.stringify({
            canonical_name: editingEntity.canonical_name.trim(),
            vendor_kind: editingEntity.vendor_kind,
            specialty: editingEntity.specialty.trim(),
            phone: editingEntity.phone.trim(),
            gstin: editingEntity.gstin.trim().toUpperCase(),
            aliases: csvToArray(editingEntity.aliases_csv),
            notes: editingEntity.notes.trim(),
          }),
        });
      } else {
        await apiFetch(`/api/munim/people/${id}`, {
          method: "PATCH",
          body: JSON.stringify({
            full_name: editingEntity.full_name.trim(),
            person_type: editingEntity.person_type,
            role_tags: csvToArray(editingEntity.role_tags_csv),
            phone: editingEntity.phone.trim(),
            aliases: csvToArray(editingEntity.aliases_csv),
            payout_aliases: csvToArray(editingEntity.payout_aliases_csv),
          }),
        });
      }
      setEditingEntity(null);
      await onRefresh();
    } finally {
      setSavingEdit(false);
    }
  }

  async function handleArchiveEntity() {
    if (!editingEntity) return;
    setSavingEdit(true);
    try {
      const endpointMap = {
        customer: `/api/munim/customers/${editingEntity.id}`,
        vendor: `/api/munim/vendors/${editingEntity.id}`,
        person: `/api/munim/people/${editingEntity.id}`,
      };
      await apiFetch(endpointMap[editingEntity.kind], {
        method: "PATCH",
        body: JSON.stringify({ is_active: false }),
      });
      setEditingEntity(null);
      await onRefresh();
    } finally {
      setSavingEdit(false);
    }
  }

  async function handleSearchArchive(e) {
    e.preventDefault();
    const res = await apiFetch(
      `/api/archive?q=${encodeURIComponent(archiveQuery.trim())}`
    );
    setArchiveRows(res || []);
  }

  return (
    <div className="space-y-5 pb-28">
      {/* Sub-navigation */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 flex-wrap">
          <button
            onClick={() => setSubTab("pulse")}
            className={`min-h-[44px] px-4 rounded-full text-xs font-bold ${
              subTab === "pulse"
                ? "bg-amber-900 text-white"
                : "bg-white border border-stone-200 text-stone-700"
            }`}
          >
            Owner Pulse (Zoho Mirror)
          </button>
          <button
            onClick={() => setSubTab("ledger")}
            className={`min-h-[44px] px-4 rounded-full text-xs font-bold ${
              subTab === "ledger"
                ? "bg-amber-900 text-white"
                : "bg-white border border-stone-200 text-stone-700"
            }`}
          >
            Ledger / Passbook
          </button>
          <button
            onClick={() => setSubTab("directory")}
            className={`min-h-[44px] px-4 rounded-full text-xs font-bold ${
              subTab === "directory"
                ? "bg-amber-900 text-white"
                : "bg-white border border-stone-200 text-stone-700"
            }`}
          >
            Shared Master Directory &amp; Archive
          </button>
        </div>

        <button
          onClick={handleSyncZoho}
          disabled={syncing}
          className="min-h-[42px] px-3.5 rounded-xl bg-white border border-stone-300 text-xs font-semibold text-stone-800 flex items-center gap-1.5"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${syncing ? "animate-spin" : ""}`} />
          <span>{syncing ? "Syncing Zoho..." : "Sync Zoho Balances"}</span>
        </button>
      </div>

      {subTab === "pulse" ? (
        <div className="space-y-5">
          {/* Cash Galla + Supervisor Floats */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="bg-white rounded-2xl border border-amber-200 p-4 space-y-2 shadow-sm">
              <div className="flex items-center gap-2 text-xs font-bold uppercase text-amber-900">
                <Wallet className="w-4 h-4" />
                <span>Shop Cash Galla</span>
              </div>
              {cashGalla.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => openLedgerForAccount(c.display_name)}
                  className="w-full min-h-[44px] text-left flex items-baseline justify-between rounded-xl px-2 py-1 hover:bg-amber-50/70 transition cursor-pointer"
                  title={`View ${c.display_name} passbook`}
                >
                  <span className="text-sm font-semibold text-stone-700 underline decoration-amber-200 underline-offset-4">
                    {c.display_name}
                  </span>
                  <span className="text-2xl font-extrabold text-stone-900">
                    {formatINR(c.cached_balance_inr)}
                  </span>
                </button>
              ))}
            </div>

            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2 shadow-sm">
              <div className="text-xs font-bold uppercase text-stone-600">
                Supervisor Floats &amp; Wallets
              </div>
              <div className="grid grid-cols-2 gap-2">
                {floats.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    onClick={() => openLedgerForAccount(f.display_name)}
                    className="w-full min-h-[48px] text-left p-2.5 rounded-xl bg-stone-50 border border-stone-200 hover:border-amber-300 hover:bg-amber-50/60 transition cursor-pointer"
                  >
                    <div className="text-xs text-stone-600 truncate">
                      {f.display_name}
                    </div>
                    <div className="text-sm font-bold text-stone-900">
                      {formatINR(f.cached_balance_inr)}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* 8 Banks & OD */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm">
            <div className="flex items-center gap-2 text-xs font-bold uppercase text-stone-600">
              <Building2 className="w-4 h-4" />
              <span>Bank, OD &amp; UPI Accounts (Tap any account for Passbook)</span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2.5">
              {banks.map((b) => (
                <button
                  key={b.id}
                  type="button"
                  onClick={() => openLedgerForAccount(b.display_name)}
                  className="w-full min-h-[52px] text-left p-3 rounded-xl bg-stone-50 border border-stone-200 hover:border-amber-300 hover:bg-amber-50/60 transition cursor-pointer"
                >
                  <div className="text-xs font-medium text-stone-600 truncate">
                    {b.display_name}
                  </div>
                  <div className="text-sm font-bold text-stone-900 mt-0.5">
                    {formatINR(b.cached_balance_inr)}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Order Receivables & P&L */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm">
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-600">
              Customer Debtors &amp; Order P&amp;L ({pnlRows.length})
            </h3>
            {pnlRows.length === 0 ? (
              <p className="text-xs text-stone-500">
                No active customer orders yet.
              </p>
            ) : (
              <div className="space-y-2">
                {pnlRows.map((r) => (
                  <div
                    key={r.project_id}
                    className="p-3 rounded-xl bg-stone-50 border border-stone-200 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs"
                  >
                    <div>
                      <span className="font-mono font-bold text-amber-900">
                        #{r.project_code}
                      </span>{" "}
                      <b className="text-sm text-stone-900">{r.customer_name}</b>
                      <div className="text-stone-500">
                        Quoted: {formatINR(r.quoted_total)} • Received:{" "}
                        {formatINR(r.total_revenue)} • Cost:{" "}
                        {formatINR(r.total_cost)}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="font-bold text-amber-950">
                        Balance Due: {formatINR(r.balance_pending)}
                      </div>
                      <div className="text-stone-500">
                        Net: {formatINR(r.net_profit)}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : subTab === "ledger" ? (
        <div className="space-y-4">
          {/* Ledger Filter Bar */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                setLedgerAppliedSearch(ledgerSearch.trim());
              }}
              className="flex flex-col sm:flex-row gap-2"
            >
              <div className="relative flex-1">
                <Search className="w-4 h-4 text-stone-400 absolute left-3.5 top-3.5" />
                <input
                  value={ledgerSearch}
                  onChange={(e) => {
                    const v = e.target.value;
                    setLedgerSearch(v);
                    if (!v.trim()) setLedgerAppliedSearch("");
                  }}
                  placeholder="Search Party / Worker / Note / Ref #..."
                  className="w-full min-h-[44px] pl-10 pr-3 rounded-xl border border-stone-300 text-sm"
                />
              </div>
              <select
                value={ledgerAccount}
                onChange={(e) => setLedgerAccount(e.target.value)}
                className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white"
              >
                <option value="">All Accounts</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.display_name}>
                    {a.display_name}
                  </option>
                ))}
              </select>
              <button
                type="submit"
                className="min-h-[44px] px-4 rounded-xl bg-stone-900 text-white text-xs font-bold"
              >
                Filter
              </button>
              <button
                type="button"
                disabled={ledgerLoading}
                onClick={() => loadLedger()}
                className="min-h-[44px] px-3.5 rounded-xl border border-stone-300 bg-white text-xs font-semibold text-stone-700 flex items-center justify-center gap-1.5"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${ledgerLoading ? "animate-spin" : ""}`} />
                <span>Refresh</span>
              </button>
            </form>

            {/* Entry Type Filter Pills */}
            <div className="flex items-center gap-1.5 flex-wrap">
              {[
                ["", "All"],
                ["customer_receipt", "Money In"],
                ["expense", "Expenses & Salary"],
                ["vendor_payment", "Vendor Payments"],
                ["float_transfer", "Transfers"],
                ["bill", "Bills"],
              ].map(([val, label]) => (
                <button
                  key={label}
                  type="button"
                  onClick={() => setLedgerType(val)}
                  className={`min-h-[44px] px-3.5 rounded-full text-xs font-semibold ${
                    ledgerType === val
                      ? "bg-amber-900 text-white"
                      : "bg-stone-100 text-stone-700 hover:bg-stone-200"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* Summary Strip */}
          {(() => {
            const totalIn = ledgerRows
              .filter((r) => r.direction === "in")
              .reduce((sum, r) => sum + Number(r.amount_inr || 0), 0);
            const totalOut = ledgerRows
              .filter((r) => r.direction === "out")
              .reduce((sum, r) => sum + Number(r.amount_inr || 0), 0);
            return (
              <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-3 rounded-2xl bg-amber-50/70 border border-amber-200 text-xs">
                <span className="font-semibold text-stone-700">
                  Showing <b>{ledgerRows.length}</b> entries
                  {ledgerAccount ? ` • ${ledgerAccount}` : ""}
                </span>
                <div className="flex items-center gap-4">
                  <span className="font-bold text-emerald-800">
                    Total In: {formatINR(totalIn)}
                  </span>
                  <span className="font-bold text-rose-800">
                    Total Out: {formatINR(totalOut)}
                  </span>
                </div>
              </div>
            );
          })()}

          {ledgerError && (
            <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">
              {ledgerError}
            </div>
          )}

          {/* Transaction List */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2 shadow-sm">
            {ledgerLoading && ledgerRows.length === 0 ? (
              <p className="text-xs text-stone-500 py-4 text-center">
                Loading Zoho passbook entries...
              </p>
            ) : ledgerRows.length === 0 ? (
              <p className="text-xs text-stone-500 py-4 text-center">
                No ledger entries match the current filters.
              </p>
            ) : (
              ledgerRows.map((tx) => {
                const badgeMap = {
                  in: "🟢 IN",
                  out: "🔴 OUT",
                  transfer: "🔄 Transfer",
                  bill: "🧾 Bill",
                };
                const amtColorMap = {
                  in: "text-emerald-700",
                  out: "text-rose-700",
                  transfer: "text-sky-700",
                  bill: "text-amber-900",
                };
                return (
                  <div
                    key={`${tx.entry_type}-${tx.id}-${tx.reference_number}`}
                    className="p-3 rounded-xl bg-stone-50 border border-stone-200 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs"
                  >
                    <div className="space-y-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-stone-500">
                          {formatDate(tx.date)}
                        </span>
                        <span className="px-2 py-0.5 rounded-full bg-white border border-stone-200 font-bold text-[11px]">
                          {badgeMap[tx.direction] || tx.entry_type}
                        </span>
                        <span className="text-stone-600 font-medium">
                          {tx.category_or_offset}
                        </span>
                        {tx.entry_type === "expense" && tx.customer_name && (
                          <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-900 font-semibold text-[11px]">
                            Customer: {tx.customer_name}
                          </span>
                        )}
                      </div>
                      <div className="text-sm font-bold text-stone-900 truncate">
                        {tx.party_name || tx.category_or_offset || "Workshop Entry"}
                      </div>
                      {tx.description && (
                        <div className="text-stone-600 break-words">
                          {tx.description}
                        </div>
                      )}
                      <div className="flex items-center gap-3 text-[11px] text-stone-500 flex-wrap">
                        {tx.account_name && (
                          <span>
                            Account: <b>{tx.account_name}</b>
                          </span>
                        )}
                        {tx.reference_number && (
                          <span className="font-mono">{tx.reference_number}</span>
                        )}
                      </div>
                    </div>
                    <div
                      className={`text-base font-extrabold shrink-0 sm:text-right ${
                        amtColorMap[tx.direction] || "text-stone-900"
                      }`}
                    >
                      {tx.direction === "in" ? "+" : tx.direction === "out" ? "-" : ""}
                      {formatINR(tx.amount_inr)}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      ) : (
        <div className="space-y-5">
          {/* Quick Onboarding Card (Vendor OR Standalone Customer) */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setAddMode("vendor")}
                  className={`min-h-[38px] px-3.5 rounded-xl text-xs font-bold flex items-center gap-1 ${
                    addMode === "vendor"
                      ? "bg-amber-900 text-white"
                      : "bg-stone-100 text-stone-700"
                  }`}
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>Add Vendor / Contractor</span>
                </button>
                <button
                  type="button"
                  onClick={() => setAddMode("customer")}
                  className={`min-h-[38px] px-3.5 rounded-xl text-xs font-bold flex items-center gap-1 ${
                    addMode === "customer"
                      ? "bg-amber-900 text-white"
                      : "bg-stone-100 text-stone-700"
                  }`}
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>Add Customer</span>
                </button>
              </div>
              <span className="text-[11px] text-stone-500">
                Tap any record below to edit details, bank aliases, or archive
              </span>
            </div>

            {addMode === "vendor" ? (
              <form onSubmit={handleAddVendor} className="space-y-2.5">
                <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
                  <input
                    value={vendorName}
                    onChange={(e) => setVendorName(e.target.value)}
                    placeholder="Vendor Name (e.g. Patel Ply, Manish Cushioning) *"
                    className="sm:col-span-2 min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                    required
                  />
                  <select
                    value={vendorKind}
                    onChange={(e) => setVendorKind(e.target.value)}
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white"
                  >
                    <option value="supplier">Material Supplier</option>
                    <option value="contractor">Piece-Rate Contractor</option>
                  </select>
                  <button
                    type="submit"
                    className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold"
                  >
                    + Save Vendor
                  </button>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                  <input
                    value={vendorSpec}
                    onChange={(e) => setVendorSpec(e.target.value)}
                    placeholder="Specialty (e.g. Plywood, Hafele)"
                    className="min-h-[42px] px-3 rounded-xl border border-stone-200 text-xs"
                  />
                  <input
                    value={vendorPhone}
                    onChange={(e) => setVendorPhone(e.target.value)}
                    placeholder="Phone (optional)"
                    className="min-h-[42px] px-3 rounded-xl border border-stone-200 text-xs"
                  />
                  <input
                    value={vendorGstin}
                    onChange={(e) => setVendorGstin(e.target.value)}
                    placeholder="GSTIN (optional)"
                    className="min-h-[42px] px-3 rounded-xl border border-stone-200 text-xs uppercase"
                  />
                </div>
              </form>
            ) : (
              <form
                onSubmit={handleAddCustomer}
                className="grid grid-cols-1 sm:grid-cols-4 gap-2"
              >
                <input
                  value={custName}
                  onChange={(e) => setCustName(e.target.value)}
                  placeholder="Customer Full Name *"
                  className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  required
                />
                <input
                  value={custArea}
                  onChange={(e) => setCustArea(e.target.value)}
                  placeholder="Area / City (e.g. Civil Lines, Jaipur)"
                  className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                />
                <input
                  value={custPhone}
                  onChange={(e) => setCustPhone(e.target.value)}
                  placeholder="WhatsApp / Phone"
                  className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                />
                <button
                  type="submit"
                  className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold"
                >
                  + Save Customer
                </button>
              </form>
            )}
          </div>

          {/* Instant Filter Across All 3 Master Lists */}
          <div className="relative">
            <Search className="w-4 h-4 text-stone-400 absolute left-3.5 top-3.5" />
            <input
              value={dirSearch}
              onChange={(e) => setDirSearch(e.target.value)}
              placeholder="Filter Customers, Vendors, Mistris, Phone, or Bank Proxy Aliases..."
              className="w-full min-h-[44px] pl-10 pr-4 rounded-2xl border border-stone-300 bg-white text-sm"
            />
          </div>

          {/* Directory Summary Cards (Tap to Edit!) */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2">
              <div className="text-xs font-bold uppercase text-stone-500">
                Customers ({filteredCustomers.length})
              </div>
              <div className="space-y-1.5 max-h-72 overflow-y-auto text-xs">
                {filteredCustomers.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => openEditModal("customer", c)}
                    className="w-full text-left p-2.5 rounded-xl bg-stone-50 hover:bg-amber-50/70 border border-transparent hover:border-amber-200 transition flex items-start justify-between gap-2"
                  >
                    <div>
                      <b>{c.full_name}</b>{" "}
                      {c.area_landmark && (
                        <span className="text-stone-500">({c.area_landmark})</span>
                      )}
                      {c.phone && (
                        <div className="text-[11px] text-stone-500">{c.phone}</div>
                      )}
                    </div>
                    <Pencil className="w-3.5 h-3.5 text-stone-400 shrink-0 mt-0.5" />
                  </button>
                ))}
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2">
              <div className="text-xs font-bold uppercase text-stone-500">
                Vendors &amp; Contractors ({filteredVendors.length})
              </div>
              <div className="space-y-1.5 max-h-72 overflow-y-auto text-xs">
                {filteredVendors.map((v) => (
                  <button
                    key={v.id}
                    type="button"
                    onClick={() => openEditModal("vendor", v)}
                    className="w-full text-left p-2.5 rounded-xl bg-stone-50 hover:bg-amber-50/70 border border-transparent hover:border-amber-200 transition flex items-start justify-between gap-2"
                  >
                    <div>
                      <b>{v.canonical_name}</b>{" "}
                      <span className="text-stone-500">• {v.vendor_kind}</span>
                      {(v.specialty || v.phone) && (
                        <div className="text-[11px] text-stone-500">
                          {[v.specialty, v.phone].filter(Boolean).join(" • ")}
                        </div>
                      )}
                    </div>
                    <Pencil className="w-3.5 h-3.5 text-stone-400 shrink-0 mt-0.5" />
                  </button>
                ))}
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2">
              <div className="text-xs font-bold uppercase text-stone-500">
                People &amp; Proxy Aliases ({filteredPeople.length})
              </div>
              <div className="space-y-1.5 max-h-72 overflow-y-auto text-xs">
                {filteredPeople.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => openEditModal("person", p)}
                    className="w-full text-left p-2.5 rounded-xl bg-stone-50 hover:bg-amber-50/70 border border-transparent hover:border-amber-200 transition flex items-start justify-between gap-2"
                  >
                    <div>
                      <b>{p.full_name}</b>{" "}
                      <span className="text-stone-500">• {p.person_type}</span>
                      {p.payout_aliases?.length > 0 && (
                        <div className="text-[11px] text-amber-800">
                          Bank Alias: {p.payout_aliases.join(", ")}
                        </div>
                      )}
                    </div>
                    <Pencil className="w-3.5 h-3.5 text-stone-400 shrink-0 mt-0.5" />
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Pre-Cutoff Historical Bill Archive Search */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3">
            <form onSubmit={handleSearchArchive} className="flex gap-2">
              <input
                value={archiveQuery}
                onChange={(e) => setArchiveQuery(e.target.value)}
                placeholder="Search pre-cutoff legacy bills by party or invoice #..."
                className="flex-1 min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
              />
              <button
                type="submit"
                className="min-h-[44px] px-4 rounded-xl bg-stone-900 text-white text-xs font-bold flex items-center gap-1.5"
              >
                <Search className="w-4 h-4" />
                <span>Search</span>
              </button>
            </form>
            {archiveRows.length > 0 && (
              <div className="space-y-1.5 text-xs">
                {archiveRows.map((a) => (
                  <div
                    key={a.id}
                    className="p-2.5 rounded-xl bg-stone-50 flex justify-between"
                  >
                    <span>
                      <b>{a.party_name}</b> • #{a.invoice_no || "N/A"} (
                      {formatDate(a.doc_date)})
                    </span>
                    <span className="font-bold">{formatINR(a.amount_inr)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Tap-to-Edit & Soft-Archive Modal */}
      {editingEntity && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-3">
          <form
            onSubmit={handleSaveEntityEdit}
            className="bg-white rounded-2xl border border-stone-200 w-full max-w-md p-4 space-y-3 shadow-2xl"
          >
            <div className="flex items-center justify-between border-b border-stone-100 pb-2">
              <h3 className="font-bold text-sm text-stone-900 uppercase">
                Edit {editingEntity.kind}
              </h3>
              <button
                type="button"
                onClick={() => setEditingEntity(null)}
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-4 h-4 text-stone-600" />
              </button>
            </div>

            {editingEntity.kind === "customer" && (
              <div className="space-y-2.5">
                <input
                  value={editingEntity.full_name}
                  onChange={(e) =>
                    setEditingEntity({ ...editingEntity, full_name: e.target.value })
                  }
                  placeholder="Customer Full Name *"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  required
                />
                <div className="grid grid-cols-2 gap-2">
                  <input
                    value={editingEntity.area_landmark}
                    onChange={(e) =>
                      setEditingEntity({
                        ...editingEntity,
                        area_landmark: e.target.value,
                      })
                    }
                    placeholder="Area / City (e.g. Civil Lines)"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  />
                  <input
                    value={editingEntity.phone}
                    onChange={(e) =>
                      setEditingEntity({ ...editingEntity, phone: e.target.value })
                    }
                    placeholder="Phone"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  />
                </div>
                <input
                  value={editingEntity.aliases_csv}
                  onChange={(e) =>
                    setEditingEntity({
                      ...editingEntity,
                      aliases_csv: e.target.value,
                    })
                  }
                  placeholder="Voice Aliases (comma-separated)"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                />
                <textarea
                  rows={2}
                  value={editingEntity.notes}
                  onChange={(e) =>
                    setEditingEntity({ ...editingEntity, notes: e.target.value })
                  }
                  placeholder="Notes (syncs to Zoho if linked)"
                  className="w-full p-3 rounded-xl border border-stone-300 text-sm"
                />
              </div>
            )}

            {editingEntity.kind === "vendor" && (
              <div className="space-y-2.5">
                <input
                  value={editingEntity.canonical_name}
                  onChange={(e) =>
                    setEditingEntity({
                      ...editingEntity,
                      canonical_name: e.target.value,
                    })
                  }
                  placeholder="Vendor / Contractor Name *"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  required
                />
                <div className="grid grid-cols-2 gap-2">
                  <select
                    value={editingEntity.vendor_kind}
                    onChange={(e) =>
                      setEditingEntity({
                        ...editingEntity,
                        vendor_kind: e.target.value,
                      })
                    }
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white"
                  >
                    <option value="supplier">Material Supplier</option>
                    <option value="contractor">Piece-Rate Contractor</option>
                  </select>
                  <input
                    value={editingEntity.specialty}
                    onChange={(e) =>
                      setEditingEntity({
                        ...editingEntity,
                        specialty: e.target.value,
                      })
                    }
                    placeholder="Specialty"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  />
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <input
                    value={editingEntity.phone}
                    onChange={(e) =>
                      setEditingEntity({ ...editingEntity, phone: e.target.value })
                    }
                    placeholder="Phone"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  />
                  <input
                    value={editingEntity.gstin}
                    onChange={(e) =>
                      setEditingEntity({ ...editingEntity, gstin: e.target.value })
                    }
                    placeholder="GSTIN"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm uppercase"
                  />
                </div>
                <input
                  value={editingEntity.aliases_csv}
                  onChange={(e) =>
                    setEditingEntity({
                      ...editingEntity,
                      aliases_csv: e.target.value,
                    })
                  }
                  placeholder="Voice Aliases (comma-separated)"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                />
                <textarea
                  rows={2}
                  value={editingEntity.notes}
                  onChange={(e) =>
                    setEditingEntity({ ...editingEntity, notes: e.target.value })
                  }
                  placeholder="Notes"
                  className="w-full p-3 rounded-xl border border-stone-300 text-sm"
                />
              </div>
            )}

            {editingEntity.kind === "person" && (
              <div className="space-y-2.5">
                <input
                  value={editingEntity.full_name}
                  onChange={(e) =>
                    setEditingEntity({ ...editingEntity, full_name: e.target.value })
                  }
                  placeholder="Person / Mistri Full Name *"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  required
                />
                <div className="grid grid-cols-2 gap-2">
                  <select
                    value={editingEntity.person_type}
                    onChange={(e) =>
                      setEditingEntity({
                        ...editingEntity,
                        person_type: e.target.value,
                      })
                    }
                    className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 text-xs bg-white"
                  >
                    <option value="salaried_karigar">Salaried Karigar / Mistri</option>
                    <option value="contractor_mistri">Contractor Mistri</option>
                    <option value="operator">Office Operator</option>
                  </select>
                  <input
                    value={editingEntity.phone}
                    onChange={(e) =>
                      setEditingEntity({ ...editingEntity, phone: e.target.value })
                    }
                    placeholder="WhatsApp Phone"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                  />
                </div>
                <input
                  value={editingEntity.role_tags_csv}
                  onChange={(e) =>
                    setEditingEntity({
                      ...editingEntity,
                      role_tags_csv: e.target.value,
                    })
                  }
                  placeholder="Role Tags (e.g. mistri, estimator, qc)"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                />
                <input
                  value={editingEntity.aliases_csv}
                  onChange={(e) =>
                    setEditingEntity({
                      ...editingEntity,
                      aliases_csv: e.target.value,
                    })
                  }
                  placeholder="Nicknames / Voice Aliases (comma-separated)"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
                />
                <input
                  value={editingEntity.payout_aliases_csv}
                  onChange={(e) =>
                    setEditingEntity({
                      ...editingEntity,
                      payout_aliases_csv: e.target.value,
                    })
                  }
                  placeholder="Bank Proxy Payee Aliases (e.g. Ruksar Bano)"
                  className="w-full min-h-[44px] px-3 rounded-xl border border-amber-300 bg-amber-50/40 text-sm"
                />
              </div>
            )}

            <div className="flex items-center justify-between gap-2 pt-2 border-t border-stone-100">
              {!confirmArchive ? (
                <button
                  type="button"
                  onClick={() => setConfirmArchive(true)}
                  className="min-h-[42px] px-3 rounded-xl border border-rose-200 bg-rose-50 text-rose-700 text-xs font-semibold flex items-center gap-1"
                >
                  <Archive className="w-3.5 h-3.5" />
                  <span>Archive</span>
                </button>
              ) : (
                <button
                  type="button"
                  disabled={savingEdit}
                  onClick={handleArchiveEntity}
                  className="min-h-[42px] px-3 rounded-xl bg-rose-700 text-white text-xs font-bold"
                >
                  Confirm Soft-Archive
                </button>
              )}

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setEditingEntity(null)}
                  className="min-h-[42px] px-3.5 rounded-xl border border-stone-300 text-xs font-semibold text-stone-700"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={savingEdit}
                  className="min-h-[42px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold"
                >
                  {savingEdit ? "Saving..." : "Save Changes"}
                </button>
              </div>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
