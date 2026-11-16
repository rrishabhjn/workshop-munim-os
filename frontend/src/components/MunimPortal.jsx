import { useState } from "react";
import { Building2, RefreshCw, Search, Wallet } from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";

export default function MunimPortal({ munimData, onRefresh }) {
  const [subTab, setSubTab] = useState("pulse");
  const [syncing, setSyncing] = useState(false);
  const [vendorName, setVendorName] = useState("");
  const [vendorKind, setVendorKind] = useState("supplier");
  const [vendorSpec, setVendorSpec] = useState("");
  const [archiveQuery, setArchiveQuery] = useState("");
  const [archiveRows, setArchiveRows] = useState([]);

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
      }),
    });
    setVendorName("");
    setVendorSpec("");
    await onRefresh();
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
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => setSubTab("pulse")}
            className={`min-h-[42px] px-4 rounded-full text-xs font-bold ${
              subTab === "pulse"
                ? "bg-amber-900 text-white"
                : "bg-white border border-stone-200 text-stone-700"
            }`}
          >
            Owner Pulse (Zoho Mirror)
          </button>
          <button
            onClick={() => setSubTab("directory")}
            className={`min-h-[42px] px-4 rounded-full text-xs font-bold ${
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
                <div key={c.id} className="flex items-baseline justify-between">
                  <span className="text-sm font-semibold text-stone-700">
                    {c.display_name}
                  </span>
                  <span className="text-2xl font-extrabold text-stone-900">
                    {formatINR(c.cached_balance_inr)}
                  </span>
                </div>
              ))}
            </div>

            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2 shadow-sm">
              <div className="text-xs font-bold uppercase text-stone-600">
                Supervisor Floats &amp; Wallets
              </div>
              <div className="grid grid-cols-2 gap-2">
                {floats.map((f) => (
                  <div
                    key={f.id}
                    className="p-2.5 rounded-xl bg-stone-50 border border-stone-200"
                  >
                    <div className="text-xs text-stone-600 truncate">
                      {f.display_name}
                    </div>
                    <div className="text-sm font-bold text-stone-900">
                      {formatINR(f.cached_balance_inr)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* 8 Banks & OD */}
          <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm">
            <div className="flex items-center gap-2 text-xs font-bold uppercase text-stone-600">
              <Building2 className="w-4 h-4" />
              <span>Bank, OD &amp; UPI Accounts (Zoho Standard Management Book)</span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2.5">
              {banks.map((b) => (
                <div
                  key={b.id}
                  className="p-3 rounded-xl bg-stone-50 border border-stone-200"
                >
                  <div className="text-xs font-medium text-stone-600 truncate">
                    {b.display_name}
                  </div>
                  <div className="text-sm font-bold text-stone-900 mt-0.5">
                    {formatINR(b.cached_balance_inr)}
                  </div>
                </div>
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
      ) : (
        <div className="space-y-5">
          {/* Quick Add Vendor / Contractor */}
          <form
            onSubmit={handleAddVendor}
            className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm"
          >
            <div className="text-xs font-bold uppercase text-stone-600">
              Add Supplier or Piece-Rate Contractor (Zoho Vendor Spine)
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
              <input
                value={vendorName}
                onChange={(e) => setVendorName(e.target.value)}
                placeholder="Vendor Name (e.g. Patel Ply, Manish Cushioning)"
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
            <input
              value={vendorSpec}
              onChange={(e) => setVendorSpec(e.target.value)}
              placeholder="Specialty (optional, e.g. Plywood, Hafele Hardware, False Ceiling)"
              className="w-full min-h-[40px] px-3 rounded-xl border border-stone-200 text-xs"
            />
          </form>

          {/* Directory Summary Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2">
              <div className="text-xs font-bold uppercase text-stone-500">
                Customers ({customers.length})
              </div>
              <div className="space-y-1.5 max-h-56 overflow-y-auto text-xs">
                {customers.map((c) => (
                  <div key={c.id} className="p-2 rounded-lg bg-stone-50">
                    <b>{c.full_name}</b>{" "}
                    {c.area_landmark && (
                      <span className="text-stone-500">({c.area_landmark})</span>
                    )}
                  </div>
                ))}
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2">
              <div className="text-xs font-bold uppercase text-stone-500">
                Vendors &amp; Contractors ({vendors.length})
              </div>
              <div className="space-y-1.5 max-h-56 overflow-y-auto text-xs">
                {vendors.map((v) => (
                  <div key={v.id} className="p-2 rounded-lg bg-stone-50">
                    <b>{v.canonical_name}</b>{" "}
                    <span className="text-stone-500">• {v.vendor_kind}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-2">
              <div className="text-xs font-bold uppercase text-stone-500">
                People &amp; Proxy Aliases ({people.length})
              </div>
              <div className="space-y-1.5 max-h-56 overflow-y-auto text-xs">
                {people.map((p) => (
                  <div key={p.id} className="p-2 rounded-lg bg-stone-50">
                    <b>{p.full_name}</b>{" "}
                    <span className="text-stone-500">• {p.person_type}</span>
                    {p.payout_aliases?.length > 0 && (
                      <div className="text-[11px] text-amber-800">
                        Bank Alias: {p.payout_aliases.join(", ")}
                      </div>
                    )}
                  </div>
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
    </div>
  );
}
