import { useState } from "react";
import { Plus, Calendar, ChevronRight } from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";

export default function ProjectsBoard({ projects, stages, onSelectProject, onRefresh }) {
  const [filter, setFilter] = useState("active");
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState({
    project_code: "",
    customer_name: "",
    phone: "",
    quoted_total: "",
    promised_delivery_date: "",
  });
  const [saving, setSaving] = useState(false);

  const stageLabelMap = Object.fromEntries(stages.map((s) => [s.code, s.label]));
  const todayStr = new Date().toISOString().slice(0, 10);

  const filtered = projects.filter((p) => {
    if (p.project_type === "general_stock") return false;
    if (filter === "overdue") {
      return (
        p.status === "active" &&
        p.promised_delivery_date &&
        p.promised_delivery_date < todayStr
      );
    }
    if (filter === "all") return true;
    return p.status === filter;
  });

  async function handleCreate(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await apiFetch("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          project_code: form.project_code.trim(),
          customer_name: form.customer_name.trim(),
          phone: form.phone.trim() || null,
          quoted_total: Number(form.quoted_total || 0),
          promised_delivery_date: form.promised_delivery_date || null,
        }),
      });
      setShowNew(false);
      setForm({
        project_code: "",
        customer_name: "",
        phone: "",
        quoted_total: "",
        promised_delivery_date: "",
      });
      onRefresh();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-2 overflow-x-auto">
          {[
            { id: "active", label: "Active" },
            { id: "overdue", label: "Overdue" },
            { id: "completed", label: "Completed" },
            { id: "all", label: "All" },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setFilter(tab.id)}
              className={`px-4 py-2 rounded-full text-sm font-medium transition ${
                filter === tab.id
                  ? "bg-amber-900 text-white"
                  : "bg-white border border-stone-200 text-stone-700"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <button
          onClick={() => setShowNew(true)}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-amber-800 text-white text-sm font-semibold shadow-sm"
        >
          <Plus className="w-4 h-4" /> New Customer
        </button>
      </div>

      {filtered.length === 0 ? (
        <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">
          No customer projects in this view yet.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {filtered.map((p) => {
            const totalRev = Number(p.total_revenue || 0);
            const quoted = Number(p.quoted_total || 0);
            const pct = quoted > 0 ? Math.min(100, Math.round((totalRev / quoted) * 100)) : 0;
            const isOverdue =
              p.status === "active" &&
              p.promised_delivery_date &&
              p.promised_delivery_date < todayStr;

            return (
              <button
                key={p.project_id}
                onClick={() => onSelectProject(p.project_id)}
                className="text-left bg-white rounded-2xl border border-stone-200 p-4 shadow-sm hover:border-amber-600 transition flex flex-col gap-3"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <span className="text-xs font-mono text-stone-500">{p.project_code}</span>
                    <h3 className="text-lg font-bold text-stone-900">{p.customer_name}</h3>
                  </div>
                  <div className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-full bg-stone-100 text-stone-700">
                    <Calendar className="w-3.5 h-3.5" />
                    <span className={isOverdue ? "text-red-700 font-bold" : ""}>
                      {formatDate(p.promised_delivery_date)}
                    </span>
                    <ChevronRight className="w-4 h-4 text-stone-400" />
                  </div>
                </div>

                <div>
                  <div className="flex justify-between text-xs text-stone-600 mb-1">
                    <span>Paid (Formal + Cash)</span>
                    <span className="font-semibold text-stone-900">
                      {formatINR(totalRev)} / {formatINR(quoted)}
                    </span>
                  </div>
                  <div className="h-2 bg-stone-100 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-emerald-600 rounded-full"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>

                <div className="flex flex-wrap gap-1.5">
                  {(p.items || []).map((it) => (
                    <span
                      key={it.id}
                      className="text-xs px-2.5 py-1 rounded-lg bg-amber-50 border border-amber-200 text-amber-950 font-medium"
                    >
                      {it.item_name}{" "}
                      <span className="text-amber-700">
                        [{stageLabelMap[it.stage] || it.stage}]
                      </span>
                    </span>
                  ))}
                </div>
              </button>
            );
          })}
        </div>
      )}

      {showNew && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-end sm:items-center justify-center p-4">
          <form
            onSubmit={handleCreate}
            className="bg-white w-full max-w-md rounded-2xl p-5 space-y-3 shadow-xl"
          >
            <h3 className="text-lg font-bold">New Customer Project</h3>
            <input
              required
              placeholder="Project Code (e.g. PRJ-2026-042)"
              value={form.project_code}
              onChange={(e) => setForm({ ...form, project_code: e.target.value })}
              className="w-full border border-stone-300 rounded-xl px-3 py-2.5"
            />
            <input
              required
              placeholder="Customer Name (e.g. Sharma Ji)"
              value={form.customer_name}
              onChange={(e) => setForm({ ...form, customer_name: e.target.value })}
              className="w-full border border-stone-300 rounded-xl px-3 py-2.5"
            />
            <input
              placeholder="Phone Number"
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
              className="w-full border border-stone-300 rounded-xl px-3 py-2.5"
            />
            <div className="grid grid-cols-2 gap-2">
              <input
                type="number"
                placeholder="Quoted Total (₹)"
                value={form.quoted_total}
                onChange={(e) => setForm({ ...form, quoted_total: e.target.value })}
                className="w-full border border-stone-300 rounded-xl px-3 py-2.5"
              />
              <input
                type="date"
                value={form.promised_delivery_date}
                onChange={(e) =>
                  setForm({ ...form, promised_delivery_date: e.target.value })
                }
                className="w-full border border-stone-300 rounded-xl px-3 py-2.5"
              />
            </div>
            <div className="flex gap-2 pt-2">
              <button
                type="button"
                onClick={() => setShowNew(false)}
                className="flex-1 py-2.5 rounded-xl border border-stone-300 font-medium"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="flex-1 py-2.5 rounded-xl bg-amber-800 text-white font-semibold"
              >
                {saving ? "Saving..." : "Create"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
