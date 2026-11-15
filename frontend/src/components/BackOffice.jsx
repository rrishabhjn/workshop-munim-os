import { useEffect, useState } from "react";
import { Download, Search, CheckCircle2, Circle, Pencil, Plus, Trash2 } from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";

export default function BackOffice() {
  const [subTab, setSubTab] = useState("cash");
  const [cashData, setCashData] = useState({ balance_inr: 0, rows: [] });
  const [followups, setFollowups] = useState([]);
  const [entities, setEntities] = useState([]);
  const [memoryRules, setMemoryRules] = useState([]);
  const [archiveRows, setArchiveRows] = useState([]);
  const [archiveQuery, setArchiveQuery] = useState("");
  const [editingNoteId, setEditingNoteId] = useState(null);
  const [editingNoteText, setEditingNoteText] = useState("");
  const [editingEntityId, setEditingEntityId] = useState(null);
  const [entityForm, setEntityForm] = useState({
    canonical_name: "",
    entity_type: "supplier",
    aliases: "",
    notes: "",
  });
  const [newRuleText, setNewRuleText] = useState("");

  async function loadAll() {
    const [c, f, e, a, m] = await Promise.all([
      apiFetch("/api/cash-ledger"),
      apiFetch("/api/followups"),
      apiFetch("/api/entities"),
      apiFetch(`/api/archive?q=${encodeURIComponent(archiveQuery)}`),
      apiFetch("/api/memory").catch(() => []),
    ]);
    setCashData(c);
    setFollowups(f);
    setEntities(e);
    setArchiveRows(a);
    setMemoryRules(m);
  }

  useEffect(() => {
    loadAll();
  }, []);

  async function toggleCashVerify(row) {
    await apiFetch(`/api/cash-ledger/${row.id}`, {
      method: "PATCH",
      body: JSON.stringify({ verified_by_staff: !row.verified_by_staff }),
    });
    loadAll();
  }

  async function saveCashNote(rowId) {
    await apiFetch(`/api/cash-ledger/${rowId}`, {
      method: "PATCH",
      body: JSON.stringify({ notes: editingNoteText.trim() }),
    });
    setEditingNoteId(null);
    setEditingNoteText("");
    loadAll();
  }

  async function saveEntity(e) {
    e.preventDefault();
    if (!entityForm.canonical_name.trim()) return;
    const payload = {
      canonical_name: entityForm.canonical_name.trim(),
      entity_type: entityForm.entity_type,
      aliases: entityForm.aliases
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      notes: entityForm.notes.trim() || null,
    };
    if (editingEntityId) {
      await apiFetch(`/api/entities/${editingEntityId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
    } else {
      await apiFetch("/api/entities", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }
    setEditingEntityId(null);
    setEntityForm({ canonical_name: "", entity_type: "supplier", aliases: "", notes: "" });
    loadAll();
  }

  async function addMemoryRule(e) {
    e.preventDefault();
    if (!newRuleText.trim()) return;
    await apiFetch("/api/memory", {
      method: "POST",
      body: JSON.stringify({ rule_text: newRuleText.trim(), category: "style" }),
    });
    setNewRuleText("");
    loadAll();
  }

  async function deleteMemoryRule(id) {
    await apiFetch(`/api/memory/${id}`, { method: "DELETE" });
    loadAll();
  }

  async function toggleFollowup(f) {
    await apiFetch(`/api/followups/${f.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: f.status === "open" ? "done" : "open" }),
    });
    loadAll();
  }

  async function handleArchiveSearch(e) {
    e.preventDefault();
    const res = await apiFetch(`/api/archive?q=${encodeURIComponent(archiveQuery)}`);
    setArchiveRows(res);
  }

  async function openArchiveFile(storagePath) {
    const res = await apiFetch(`/api/media/signed-url?path=${encodeURIComponent(storagePath)}`);
    window.open(res.url, "_blank", "noopener,noreferrer");
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2 overflow-x-auto">
        {[
          { id: "cash", label: "Cash & Karigar" },
          { id: "followups", label: "Follow-Ups" },
          { id: "entities", label: "Dictionary" },
          { id: "memory", label: "Bot Memory" },
          { id: "archive", label: "Archive" },
        ].map((t) => (
          <button
            key={t.id}
            onClick={() => setSubTab(t.id)}
            className={`px-4 py-2 rounded-full text-sm font-medium whitespace-nowrap ${
              subTab === t.id
                ? "bg-amber-900 text-white"
                : "bg-white border border-stone-200 text-stone-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {subTab === "cash" && (
        <div className="space-y-3">
          <div className="bg-white rounded-2xl border border-stone-200 p-4 flex items-center justify-between">
            <div>
              <div className="text-xs text-stone-500">Running Cash-Box Balance</div>
              <div className="text-2xl font-extrabold text-stone-900">
                {formatINR(cashData.balance_inr)}
              </div>
            </div>
            <a
              href="/api/cash-ledger/export.csv"
              className="inline-flex items-center gap-1.5 px-4 py-2.5 rounded-xl bg-stone-100 text-sm font-semibold text-stone-800"
            >
              <Download className="w-4 h-4" /> Export CSV
            </a>
          </div>

          <div className="space-y-2">
            {cashData.rows.map((r) => (
              <div
                key={r.id}
                className="bg-white rounded-xl border border-stone-200 p-3 flex items-center justify-between gap-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="font-bold text-sm">
                    {r.party_or_karigar}{" "}
                    <span className="text-xs font-normal text-stone-500">
                      ({r.txn_type})
                    </span>
                  </div>
                  {editingNoteId === r.id ? (
                    <div className="mt-1.5 flex gap-2">
                      <input
                        value={editingNoteText}
                        onChange={(e) => setEditingNoteText(e.target.value)}
                        placeholder="Add description / details..."
                        className="flex-1 bg-stone-50 border border-stone-300 rounded-lg px-2.5 py-1.5 text-xs"
                      />
                      <button
                        onClick={() => saveCashNote(r.id)}
                        className="px-3 py-1.5 rounded-lg bg-amber-800 text-white text-xs font-semibold"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingNoteId(null)}
                        className="px-2.5 py-1.5 rounded-lg bg-stone-100 text-stone-700 text-xs"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div className="text-xs text-stone-500 flex items-center gap-1.5">
                      <span>
                        {formatDate(r.txn_date)} • {r.notes || "No note"}
                      </span>
                      <button
                        onClick={() => {
                          setEditingNoteId(r.id);
                          setEditingNoteText(r.notes || "");
                        }}
                        title="Edit note"
                        className="p-1 text-stone-400 hover:text-amber-800"
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-3">
                  <span
                    className={`font-bold text-sm ${
                      r.direction === "in" ? "text-emerald-700" : "text-rose-700"
                    }`}
                  >
                    {r.direction === "in" ? "+" : "-"}
                    {formatINR(r.amount_inr)}
                  </span>
                  <button
                    onClick={() => toggleCashVerify(r)}
                    title="Verify entry"
                    className="p-2 rounded-lg text-stone-600"
                  >
                    {r.verified_by_staff ? (
                      <CheckCircle2 className="w-5 h-5 text-emerald-600" />
                    ) : (
                      <Circle className="w-5 h-5 text-stone-300" />
                    )}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {subTab === "followups" && (
        <div className="space-y-2">
          {followups.map((f) => (
            <div
              key={f.id}
              className="bg-white rounded-xl border border-stone-200 p-3 flex items-center justify-between gap-3"
            >
              <div>
                <div className="font-bold text-sm">
                  {f.name}{" "}
                  <span className="text-xs font-normal text-stone-500">
                    • Due {formatDate(f.due_date)}
                  </span>
                </div>
                <div className="text-xs text-stone-600">{f.action_note}</div>
              </div>
              <button
                onClick={() => toggleFollowup(f)}
                className={`px-3 py-1.5 rounded-lg text-xs font-semibold ${
                  f.status === "done"
                    ? "bg-emerald-100 text-emerald-800"
                    : "bg-amber-100 text-amber-900"
                }`}
              >
                {f.status === "done" ? "Done" : "Mark Done"}
              </button>
            </div>
          ))}
        </div>
      )}

      {subTab === "entities" && (
        <div className="space-y-3">
          <form onSubmit={saveEntity} className="bg-white rounded-xl border border-stone-200 p-3 space-y-2">
            <div className="text-xs font-semibold text-stone-700">
              {editingEntityId ? "Edit Dictionary Entry" : "Add Person / Supplier / Karigar"}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <input
                placeholder="Canonical name (e.g. Suresh Ji)"
                value={entityForm.canonical_name}
                onChange={(e) => setEntityForm({ ...entityForm, canonical_name: e.target.value })}
                className="bg-stone-50 border border-stone-300 rounded-lg px-3 py-2 text-sm"
              />
              <select
                value={entityForm.entity_type}
                onChange={(e) => setEntityForm({ ...entityForm, entity_type: e.target.value })}
                className="bg-stone-50 border border-stone-300 rounded-lg px-3 py-2 text-sm"
              >
                <option value="supplier">supplier</option>
                <option value="karigar">karigar</option>
                <option value="customer">customer</option>
                <option value="material">material</option>
              </select>
            </div>
            <input
              placeholder="Aliases (comma-separated, e.g. Suresh Painter, Suresh)"
              value={entityForm.aliases}
              onChange={(e) => setEntityForm({ ...entityForm, aliases: e.target.value })}
              className="w-full bg-stone-50 border border-stone-300 rounded-lg px-3 py-2 text-sm"
            />
            <input
              placeholder="Notes (e.g. Regular painting supplier, not employee)"
              value={entityForm.notes}
              onChange={(e) => setEntityForm({ ...entityForm, notes: e.target.value })}
              className="w-full bg-stone-50 border border-stone-300 rounded-lg px-3 py-2 text-sm"
            />
            <div className="flex gap-2">
              <button
                type="submit"
                className="inline-flex items-center gap-1 px-4 py-2 rounded-lg bg-amber-800 text-white text-sm font-semibold"
              >
                <Plus className="w-4 h-4" /> {editingEntityId ? "Update" : "Save"}
              </button>
              {editingEntityId && (
                <button
                  type="button"
                  onClick={() => {
                    setEditingEntityId(null);
                    setEntityForm({ canonical_name: "", entity_type: "supplier", aliases: "", notes: "" });
                  }}
                  className="px-3 py-2 rounded-lg bg-stone-100 text-stone-700 text-sm"
                >
                  Cancel
                </button>
              )}
            </div>
          </form>

          <div className="space-y-2">
            {entities.map((e) => (
              <div
                key={e.id}
                className="bg-white rounded-xl border border-stone-200 p-3 flex items-start justify-between gap-2"
              >
                <div>
                  <div className="font-bold text-sm">
                    {e.canonical_name}{" "}
                    <span className="text-xs font-normal text-stone-500">
                      ({e.entity_type})
                    </span>
                  </div>
                  <div className="text-xs text-stone-500">
                    Aliases: {(e.aliases || []).join(", ") || "—"}
                  </div>
                  {e.notes && (
                    <div className="text-xs text-amber-900 mt-1">
                      Note: {e.notes}
                    </div>
                  )}
                </div>
                <button
                  onClick={() => {
                    setEditingEntityId(e.id);
                    setEntityForm({
                      canonical_name: e.canonical_name || "",
                      entity_type: e.entity_type || "supplier",
                      aliases: (e.aliases || []).join(", "),
                      notes: e.notes || "",
                    });
                  }}
                  title="Edit entity"
                  className="p-2 text-stone-400 hover:text-amber-800"
                >
                  <Pencil className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {subTab === "memory" && (
        <div className="space-y-3">
          <form onSubmit={addMemoryRule} className="flex gap-2">
            <input
              placeholder="Teach bot a rule (e.g. 'painting ke liye' -> vendor_payment)..."
              value={newRuleText}
              onChange={(e) => setNewRuleText(e.target.value)}
              className="flex-1 bg-white border border-stone-300 rounded-xl px-3 py-2.5 text-sm"
            />
            <button
              type="submit"
              className="px-4 py-2.5 rounded-xl bg-amber-800 text-white text-sm font-semibold inline-flex items-center gap-1"
            >
              <Plus className="w-4 h-4" /> Add
            </button>
          </form>
          <div className="space-y-2">
            {memoryRules.length === 0 && (
              <div className="bg-white rounded-xl border border-stone-200 p-4 text-xs text-stone-500">
                No learned rules yet. Rules are automatically distilled when you correct a bot guess via Edit.
              </div>
            )}
            {memoryRules.map((m) => (
              <div
                key={m.id}
                className="bg-white rounded-xl border border-stone-200 p-3 flex items-center justify-between gap-3"
              >
                <div>
                  <div className="text-sm font-medium text-stone-900">{m.rule_text}</div>
                  <div className="text-xs text-stone-500">
                    {m.category} • {m.source}
                  </div>
                </div>
                <button
                  onClick={() => deleteMemoryRule(m.id)}
                  title="Delete rule"
                  className="p-2 text-stone-400 hover:text-rose-700"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {subTab === "archive" && (
        <div className="space-y-3">
          <form onSubmit={handleArchiveSearch} className="flex gap-2">
            <input
              placeholder="Search party name in pre-cutoff archive..."
              value={archiveQuery}
              onChange={(e) => setArchiveQuery(e.target.value)}
              className="flex-1 bg-white border border-stone-300 rounded-xl px-3 py-2.5 text-sm"
            />
            <button
              type="submit"
              className="px-4 py-2.5 rounded-xl bg-amber-800 text-white text-sm font-semibold inline-flex items-center gap-1"
            >
              <Search className="w-4 h-4" /> Search
            </button>
          </form>

          <div className="space-y-2">
            {archiveRows.map((a) => (
              <div
                key={a.id}
                className="bg-white rounded-xl border border-stone-200 p-3 flex items-center justify-between"
              >
                <div>
                  <div className="font-bold text-sm">{a.party_name}</div>
                  <div className="text-xs text-stone-500">
                    {a.doc_type} • {a.invoice_no || "No Inv"} • {formatDate(a.doc_date)} •{" "}
                    {formatINR(a.amount_inr)}
                  </div>
                </div>
                <button
                  onClick={() => openArchiveFile(a.storage_path)}
                  className="px-3 py-1.5 rounded-lg bg-stone-100 text-xs font-semibold"
                >
                  View File
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
