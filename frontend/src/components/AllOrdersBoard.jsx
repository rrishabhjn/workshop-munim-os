import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Archive,
  ChevronDown,
  ChevronUp,
  History,
  Pencil,
  Plus,
  Share2,
} from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import { shareKarigarJobCard } from "../lib/jobCard";

const PRE_PROD_CODES = new Set([
  "lead",
  "design",
  "estimating",
  "quoted_awaiting_advance",
  "site_sizing_boq",
]);

export default function AllOrdersBoard({
  lookups,
  people,
  orders,
  selectedOrderId,
  onSelectOrder,
  onMutated,
  onShowUndoToast,
}) {
  const [filter, setFilter] = useState("active_all");
  const [showNewLead, setShowNewLead] = useState(false);
  const [fullName, setFullName] = useState("");
  const [areaLandmark, setAreaLandmark] = useState("");
  const [roughScope, setRoughScope] = useState("");
  const [phone, setPhone] = useState("");
  const [newItemName, setNewItemName] = useState({});
  const [newItemMistri, setNewItemMistri] = useState({});
  const [timelines, setTimelines] = useState({});
  const [editingOrder, setEditingOrder] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const peopleById = Object.fromEntries((people || []).map((p) => [p.id, p]));
  const operators = (people || []).filter((p) => p.person_type === "operator");
  const mistris = (people || []).filter((p) => p.person_type !== "operator");
  const allAssignees = [...mistris, ...operators];

  const pipelineStages = lookups?.pipeline_stages || [];
  const materialStages = lookups?.material_stages || [];
  const itemStages = lookups?.item_stages || [];

  useEffect(() => {
    if (!selectedOrderId) return;
    const target = (orders || []).find((o) => o.id === selectedOrderId);
    if (!target) return;
    const isVisible =
      filter === "pre_prod"
        ? PRE_PROD_CODES.has(target.pipeline_stage)
        : filter === "execution"
        ? target.pipeline_stage === "execution"
        : filter === "packed_gate"
        ? (target.items || []).some((it) => it.stage === "packed_ready")
        : filter === "completed"
        ? target.pipeline_stage === "completed"
        : target.status === "active";
    if (!isVisible) {
      setFilter(target.pipeline_stage === "completed" ? "completed" : "active_all");
    }
    requestAnimationFrame(() => {
      document
        .getElementById(`order-card-${selectedOrderId}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }, [selectedOrderId]);

  const filteredOrders = (orders || []).filter((o) => {
    if (filter === "pre_prod") return PRE_PROD_CODES.has(o.pipeline_stage);
    if (filter === "execution") return o.pipeline_stage === "execution";
    if (filter === "packed_gate") {
      return (o.items || []).some((it) => it.stage === "packed_ready");
    }
    if (filter === "completed") return o.pipeline_stage === "completed";
    return o.status === "active";
  });

  async function handleCreateLead(e) {
    e.preventDefault();
    if (!fullName.trim() || !areaLandmark.trim() || !roughScope.trim()) return;
    setBusyId("new");
    try {
      const res = await apiFetch("/api/production/orders", {
        method: "POST",
        body: JSON.stringify({
          full_name: fullName.trim(),
          area_landmark: areaLandmark.trim(),
          rough_scope: roughScope.trim(),
          phone: phone.trim() || null,
        }),
      });
      setFullName("");
      setAreaLandmark("");
      setRoughScope("");
      setPhone("");
      setShowNewLead(false);
      await onMutated();
      if (res?.project_id) onSelectOrder(res.project_id);
    } finally {
      setBusyId(null);
    }
  }

  async function handlePipelineChange(order, toStage, extra = {}) {
    setBusyId(order.id);
    try {
      const res = await apiFetch(`/api/production/orders/${order.id}/pipeline`, {
        method: "POST",
        body: JSON.stringify({ to_stage: toStage, ...extra }),
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Stage updated", res.undo_event_ids);
      }
    } finally {
      setBusyId(null);
    }
  }

  async function handleMaterialChange(order, toStage) {
    setBusyId(order.id);
    try {
      const res = await apiFetch(`/api/production/orders/${order.id}/material`, {
        method: "POST",
        body: JSON.stringify({ to_stage: toStage }),
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Material updated", res.undo_event_ids);
      }
    } finally {
      setBusyId(null);
    }
  }

  async function handleToggleScopeChanged(order) {
    await apiFetch(`/api/production/orders/${order.id}`, {
      method: "PATCH",
      body: JSON.stringify({ boq_scope_changed: !order.boq_scope_changed }),
    });
    await onMutated();
  }

  function startEditOrderDetails(order) {
    setEditingOrder({
      id: order.id,
      quoted_total: order.quoted_total ?? 0,
      promised_delivery_date: order.promised_delivery_date || "",
      rough_scope: order.rough_scope || "",
      site_area_landmark: order.site_area_landmark || "",
      phone: order.phone || "",
      boq_scope_note: order.boq_scope_note || "",
    });
  }

  async function handleSaveOrderDetails(e) {
    e.preventDefault();
    if (!editingOrder) return;
    setBusyId(editingOrder.id);
    try {
      await apiFetch(`/api/production/orders/${editingOrder.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          quoted_total: Number(editingOrder.quoted_total || 0),
          promised_delivery_date: editingOrder.promised_delivery_date || "",
          rough_scope: editingOrder.rough_scope,
          site_area_landmark: editingOrder.site_area_landmark,
          phone: editingOrder.phone,
          boq_scope_note: editingOrder.boq_scope_note,
        }),
      });
      setEditingOrder(null);
      await onMutated();
    } finally {
      setBusyId(null);
    }
  }

  async function handleAddItem(orderId) {
    const name = (newItemName[orderId] || "").trim();
    if (!name) return;
    setBusyId(orderId);
    try {
      const res = await apiFetch(`/api/production/orders/${orderId}/items`, {
        method: "POST",
        body: JSON.stringify({
          item_name: name,
          to_stage: "in_shop",
          production_mistri_id: newItemMistri[orderId] || null,
        }),
      });
      setNewItemName((prev) => ({ ...prev, [orderId]: "" }));
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || `Added ${name}`, res.undo_event_ids);
      }
    } finally {
      setBusyId(null);
    }
  }

  async function handleItemPatch(itemId, patch) {
    const res = await apiFetch(`/api/production/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    await onMutated();
    if (res?.undo_event_ids?.length) {
      onShowUndoToast(res.receipt || "Item updated", res.undo_event_ids);
    }
  }

  async function toggleTimeline(orderId) {
    if (timelines[orderId]) {
      setTimelines((prev) => ({ ...prev, [orderId]: null }));
      return;
    }
    const rows = await apiFetch(`/api/production/orders/${orderId}/timeline`);
    setTimelines((prev) => ({ ...prev, [orderId]: rows }));
  }

  return (
    <div className="space-y-4 pb-28">
      {/* Top Filter Pills + New Lead Button */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
          {[
            { id: "active_all", label: "All Active" },
            { id: "pre_prod", label: "Pre-Prod Pipeline" },
            { id: "execution", label: "In Execution" },
            { id: "packed_gate", label: "Packed / Ready Gate" },
            { id: "completed", label: "Completed" },
          ].map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`min-h-[40px] px-3.5 py-1.5 rounded-full text-xs font-semibold whitespace-nowrap transition ${
                filter === f.id
                  ? "bg-amber-900 text-white shadow-sm"
                  : "bg-white text-stone-700 border border-stone-200 hover:bg-stone-50"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        <button
          onClick={() => setShowNewLead((v) => !v)}
          className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold flex items-center gap-1.5 shadow-sm"
        >
          <Plus className="w-4 h-4" />
          <span>New Lead</span>
        </button>
      </div>

      {/* Onboarding Card (Full Name + Site Area + Rough Scope) */}
      {showNewLead && (
        <form
          onSubmit={handleCreateLead}
          className="bg-white rounded-2xl border-2 border-amber-300 p-4 space-y-3 shadow-sm"
        >
          <div className="font-bold text-sm text-amber-950">
            New Customer Lead (Stays out of Zoho until conversion)
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            <input
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Customer Full Name *"
              className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
              required
            />
            <input
              value={areaLandmark}
              onChange={(e) => setAreaLandmark(e.target.value)}
              placeholder="Site Area / City (e.g. Civil Lines, Jaipur) *"
              className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
              required
            />
          </div>
          <input
            value={roughScope}
            onChange={(e) => setRoughScope(e.target.value)}
            placeholder="Rough Scope (e.g. Modular Kitchen + 2 Wardrobes) *"
            className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
            required
          />
          <input
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="Phone (optional)"
            className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
          />
          <p className="text-[11px] text-stone-500">
            Project code is generated automatically. Quote ₹ &amp; target delivery date can be added later once finalised.
          </p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setShowNewLead(false)}
              className="min-h-[44px] px-4 rounded-xl border border-stone-300 text-xs font-semibold text-stone-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busyId === "new"}
              className="min-h-[44px] px-5 rounded-xl bg-amber-900 text-white text-xs font-bold"
            >
              {busyId === "new" ? "Saving..." : "Save Lead"}
            </button>
          </div>
        </form>
      )}

      {/* Orders List */}
      {filteredOrders.length === 0 ? (
        <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center text-sm text-stone-500">
          No orders in this view yet. Tap <b>+ New Lead</b> or tell the AI bar{" "}
          <i>&ldquo;Manoj Sharma Ajmer Road ka naya lead daal do, modular kitchen&rdquo;</i>.
        </div>
      ) : (
        <div className="space-y-3">
          {filteredOrders.map((order) => {
            const isExpanded = selectedOrderId === order.id;
            const items = order.items || [];
            const qcDoneCount = items.filter((i) => i.stage === "qc_done").length;
            const hasPackedItem = items.some((i) => i.stage === "packed_ready");
            const showPaymentGateAlert =
              hasPackedItem && order.payment_stage !== "payment_complete";

            return (
              <div
                key={order.id}
                id={`order-card-${order.id}`}
                className="bg-white rounded-2xl border border-stone-200 shadow-sm overflow-hidden"
              >
                {/* Order Summary Header */}
                <div
                  onClick={() =>
                    onSelectOrder(isExpanded ? null : order.id)
                  }
                  className="p-4 cursor-pointer hover:bg-stone-50/70 transition space-y-2.5"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="space-y-0.5">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-mono font-bold text-amber-950 bg-amber-100 px-2 py-0.5 rounded">
                          #{order.project_code}
                        </span>
                        <h3 className="font-bold text-base text-stone-900">
                          {order.customer_name}
                        </h3>
                      </div>
                      {order.rough_scope && (
                        <p className="text-xs text-stone-600">
                          {order.rough_scope}
                        </p>
                      )}
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      {Number(order.quoted_total) > 0 && (
                        <span className="text-xs font-semibold text-stone-700 bg-stone-100 px-2.5 py-1 rounded-lg">
                          {formatINR(order.paid_cache_inr)} /{" "}
                          {formatINR(order.quoted_total)}
                        </span>
                      )}
                      {isExpanded ? (
                        <ChevronUp className="w-5 h-5 text-stone-500" />
                      ) : (
                        <ChevronDown className="w-5 h-5 text-stone-500" />
                      )}
                    </div>
                  </div>

                  {/* Parallel Track Pills */}
                  <div className="flex items-center gap-2 flex-wrap text-xs">
                    <span className="font-semibold px-2.5 py-1 rounded-full bg-amber-50 text-amber-950 border border-amber-200">
                      Stage:{" "}
                      {pipelineStages.find((s) => s.code === order.pipeline_stage)
                        ?.label || order.pipeline_stage}
                    </span>

                    {order.material_stage !== "not_started" && (
                      <span
                        className={`font-semibold px-2.5 py-1 rounded-full border ${
                          order.material_stage === "all_in_shop"
                            ? "bg-emerald-50 text-emerald-800 border-emerald-200"
                            : "bg-sky-50 text-sky-900 border-sky-200"
                        }`}
                      >
                        Ansh Material:{" "}
                        {materialStages.find(
                          (m) => m.code === order.material_stage
                        )?.label || order.material_stage}
                      </span>
                    )}

                    <span
                      className={`font-semibold px-2.5 py-1 rounded-full border ${
                        order.payment_stage === "payment_complete"
                          ? "bg-emerald-50 text-emerald-800 border-emerald-200"
                          : "bg-stone-100 text-stone-700 border-stone-200"
                      }`}
                    >
                      Payment: {order.payment_stage.replace(/_/g, " ")}
                    </span>

                    {items.length > 0 && (
                      <span className="font-semibold px-2.5 py-1 rounded-full bg-stone-100 text-stone-800">
                        Items: {qcDoneCount}/{items.length} QC Done
                      </span>
                    )}

                    {order.promised_delivery_date && (
                      <span className="text-stone-500">
                        Due {formatDate(order.promised_delivery_date)}
                      </span>
                    )}
                  </div>

                  {showPaymentGateAlert && (
                    <div className="flex items-center gap-2 text-xs font-semibold text-amber-950 bg-amber-50 border border-amber-300 rounded-xl px-3 py-2">
                      <AlertTriangle className="w-4 h-4 text-amber-700 shrink-0" />
                      <span>
                        Pre-Dispatch Gate: Item(s) are Packed / Ready while payment is{" "}
                        {order.payment_stage.replace(/_/g, " ")}.
                      </span>
                    </div>
                  )}
                </div>

                {/* Expanded Order Workspace */}
                {isExpanded && (
                  <div className="border-t border-stone-200 bg-stone-50/70 p-4 space-y-4">
                    {/* 1. Tier-1 Pipeline Selector + Role Pickers */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 bg-white p-3.5 rounded-xl border border-stone-200">
                      <div className="space-y-1">
                        <label className="text-[11px] font-bold uppercase text-stone-500">
                          Project Pipeline Stage
                        </label>
                        <select
                          value={order.pipeline_stage}
                          onChange={(e) =>
                            handlePipelineChange(order, e.target.value)
                          }
                          className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white font-semibold"
                        >
                          {pipelineStages.map((s) => (
                            <option key={s.code} value={s.code}>
                              {s.label}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div className="space-y-1">
                        <label className="text-[11px] font-bold uppercase text-stone-500">
                          Ansh Material Track (Parallel)
                        </label>
                        <select
                          value={order.material_stage}
                          onChange={(e) =>
                            handleMaterialChange(order, e.target.value)
                          }
                          className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white font-semibold"
                        >
                          {materialStages.map((m) => (
                            <option key={m.code} value={m.code}>
                              {m.label}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div className="space-y-1">
                        <label className="text-[11px] font-bold uppercase text-stone-500">
                          {order.pipeline_stage === "estimating"
                            ? "Estimator Assigned"
                            : "Sizing Mistri & Scope Gate"}
                        </label>
                        {order.pipeline_stage === "estimating" ? (
                          <select
                            value={(order.estimator_ids || [])[0] || ""}
                            onChange={(e) =>
                              handlePipelineChange(order, "estimating", {
                                estimator_ids: e.target.value
                                  ? [e.target.value]
                                  : [],
                              })
                            }
                            className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white"
                          >
                            <option value="">Assign Estimator...</option>
                            {operators.map((op) => (
                              <option key={op.id} value={op.id}>
                                {op.full_name}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <div className="flex items-center gap-2">
                            <select
                              value={order.sizing_mistri_id || ""}
                              onChange={(e) =>
                                handlePipelineChange(
                                  order,
                                  order.pipeline_stage,
                                  {
                                    sizing_mistri_id: e.target.value || null,
                                  }
                                )
                              }
                              className="flex-1 min-h-[44px] px-2.5 rounded-xl border border-stone-300 text-xs bg-white"
                            >
                              <option value="">Sizing Mistri...</option>
                              {allAssignees.map((p) => (
                                <option key={p.id} value={p.id}>
                                  {p.full_name}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              onClick={() => handleToggleScopeChanged(order)}
                              className={`min-h-[44px] px-2.5 rounded-xl border text-xs font-semibold shrink-0 ${
                                order.boq_scope_changed
                                  ? "bg-amber-100 border-amber-400 text-amber-950"
                                  : "bg-stone-50 border-stone-300 text-stone-600"
                              }`}
                            >
                              {order.boq_scope_changed
                                ? "Scope Changed ⚠️"
                                : "Matches Est ✓"}
                            </button>
                          </div>
                        )}
                      </div>
                    </div>

                    {/* 2. Tier-2 Flat Furniture Item Execution Cards */}
                    <div className="space-y-2.5">
                      <div className="flex items-center justify-between">
                        <h4 className="text-xs font-bold uppercase tracking-wider text-stone-600">
                          Furniture Items / Groups ({items.length})
                        </h4>
                      </div>

                      {items.map((it) => {
                        const shopName =
                          peopleById[it.production_mistri_id]?.full_name ||
                          it.assigned_karigar ||
                          "";
                        const siteName =
                          peopleById[it.assembly_mistri_id]?.full_name || "";

                        return (
                          <div
                            key={it.id}
                            className="bg-white rounded-xl border border-stone-200 p-3 space-y-2.5"
                          >
                            <div className="flex items-center justify-between gap-2 flex-wrap">
                              <div>
                                <div className="font-bold text-sm text-stone-900">
                                  {it.item_name}
                                </div>
                                <div className="text-xs text-stone-500 flex items-center gap-2 flex-wrap">
                                  {shopName && <span>🔨 Shop: {shopName}</span>}
                                  {siteName && <span>🏠 Site: {siteName}</span>}
                                  {it.dimensions && <span>• {it.dimensions}</span>}
                                </div>
                              </div>

                              <button
                                type="button"
                                onClick={() =>
                                  shareKarigarJobCard({
                                    projectCode: order.project_code,
                                    customerArea: order.site_area_landmark,
                                    item: it,
                                    shopMistriName: shopName,
                                    siteMistriName: siteName,
                                  })
                                }
                                className="min-h-[40px] px-3 rounded-xl border border-amber-300 bg-amber-50 text-amber-950 text-xs font-semibold flex items-center gap-1.5"
                              >
                                <Share2 className="w-3.5 h-3.5" />
                                <span>Job Card</span>
                              </button>
                            </div>

                            {/* 4-Stage Physical Location Pills */}
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
                              {itemStages.map((st) => (
                                <button
                                  key={st.code}
                                  type="button"
                                  onClick={() =>
                                    handleItemPatch(it.id, { to_stage: st.code })
                                  }
                                  className={`min-h-[40px] px-2.5 rounded-lg text-xs font-semibold border transition ${
                                    it.stage === st.code
                                      ? "bg-amber-900 text-white border-amber-900"
                                      : "bg-stone-50 text-stone-700 border-stone-200 hover:bg-stone-100"
                                  }`}
                                >
                                  {st.label}
                                </button>
                              ))}
                            </div>

                            {/* Separate Shop vs Site Mistri Pickers (Never Overwrites Shop Mistri!) */}
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-1">
                              <select
                                value={it.production_mistri_id || ""}
                                onChange={(e) =>
                                  handleItemPatch(it.id, {
                                    to_stage: it.stage,
                                    production_mistri_id: e.target.value || null,
                                  })
                                }
                                className="min-h-[40px] px-2.5 rounded-lg border border-stone-200 text-xs bg-stone-50"
                              >
                                <option value="">Shop Mistri (Production)...</option>
                                {allAssignees.map((p) => (
                                  <option key={p.id} value={p.id}>
                                    Shop: {p.full_name}
                                  </option>
                                ))}
                              </select>

                              <select
                                value={it.assembly_mistri_id || ""}
                                onChange={(e) =>
                                  handleItemPatch(it.id, {
                                    to_stage: it.stage,
                                    assembly_mistri_id: e.target.value || null,
                                  })
                                }
                                className="min-h-[40px] px-2.5 rounded-lg border border-stone-200 text-xs bg-stone-50"
                              >
                                <option value="">Site Assembly Mistri...</option>
                                {allAssignees.map((p) => (
                                  <option key={p.id} value={p.id}>
                                    Site: {p.full_name}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </div>
                        );
                      })}

                      {/* Quick Add Flat Item */}
                      <div className="flex flex-col sm:flex-row gap-2 pt-1">
                        <input
                          value={newItemName[order.id] || ""}
                          onChange={(e) =>
                            setNewItemName((prev) => ({
                              ...prev,
                              [order.id]: e.target.value,
                            }))
                          }
                          placeholder="Add flat item card (e.g. Modular Kitchen, Master Bed)"
                          className="flex-1 min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm bg-white"
                        />
                        <select
                          value={newItemMistri[order.id] || ""}
                          onChange={(e) =>
                            setNewItemMistri((prev) => ({
                              ...prev,
                              [order.id]: e.target.value,
                            }))
                          }
                          className="min-h-[44px] px-3 rounded-xl border border-stone-300 text-xs bg-white"
                        >
                          <option value="">Shop Mistri (optional)</option>
                          {allAssignees.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.full_name}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          onClick={() => handleAddItem(order.id)}
                          className="min-h-[44px] px-4 rounded-xl bg-stone-900 text-white text-xs font-bold shrink-0"
                        >
                          + Add Item
                        </button>
                      </div>
                    </div>

                    {/* 3. Order Metadata Edit + Accountability History */}
                    <div className="pt-1 space-y-2">
                      <div className="flex items-center justify-between gap-2 flex-wrap">
                        <button
                          type="button"
                          onClick={() => toggleTimeline(order.id)}
                          className="text-xs font-semibold text-stone-600 hover:text-stone-900 flex items-center gap-1.5"
                        >
                          <History className="w-3.5 h-3.5" />
                          <span>
                            {timelines[order.id]
                              ? "Hide Stage History"
                              : "View Who Did What History"}
                          </span>
                        </button>

                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() =>
                              editingOrder?.id === order.id
                                ? setEditingOrder(null)
                                : startEditOrderDetails(order)
                            }
                            className="min-h-[38px] px-3 rounded-xl border border-stone-300 bg-white text-xs font-semibold text-stone-800 flex items-center gap-1.5"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                            <span>
                              {editingOrder?.id === order.id
                                ? "Close Edit"
                                : "Edit Order Details"}
                            </span>
                          </button>
                          {order.pipeline_stage !== "lost" && (
                            <button
                              type="button"
                              onClick={() => handlePipelineChange(order, "lost")}
                              className="min-h-[38px] px-3 rounded-xl border border-rose-200 bg-rose-50 text-xs font-semibold text-rose-700 flex items-center gap-1"
                            >
                              <Archive className="w-3.5 h-3.5" />
                              <span>Mark Lost</span>
                            </button>
                          )}
                        </div>
                      </div>

                      {editingOrder?.id === order.id && (
                        <form
                          onSubmit={handleSaveOrderDetails}
                          className="bg-white rounded-xl border-2 border-amber-200 p-3.5 space-y-2.5"
                        >
                          <div className="text-xs font-bold uppercase text-amber-950">
                            Edit Order Deal, Delivery &amp; Scope
                          </div>
                          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                            <input
                              type="number"
                              value={editingOrder.quoted_total}
                              onChange={(e) =>
                                setEditingOrder({
                                  ...editingOrder,
                                  quoted_total: e.target.value,
                                })
                              }
                              placeholder="Deal Value ₹"
                              className="min-h-[42px] px-3 rounded-xl border border-stone-300 text-sm"
                            />
                            <input
                              type="date"
                              value={editingOrder.promised_delivery_date}
                              onChange={(e) =>
                                setEditingOrder({
                                  ...editingOrder,
                                  promised_delivery_date: e.target.value,
                                })
                              }
                              className="min-h-[42px] px-3 rounded-xl border border-stone-300 text-sm"
                            />
                            <input
                              value={editingOrder.phone}
                              onChange={(e) =>
                                setEditingOrder({
                                  ...editingOrder,
                                  phone: e.target.value,
                                })
                              }
                              placeholder="Customer Phone"
                              className="min-h-[42px] px-3 rounded-xl border border-stone-300 text-sm"
                            />
                          </div>
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                            <input
                              value={editingOrder.site_area_landmark}
                              onChange={(e) =>
                                setEditingOrder({
                                  ...editingOrder,
                                  site_area_landmark: e.target.value,
                                })
                              }
                              placeholder="Site Area / Landmark"
                              className="min-h-[42px] px-3 rounded-xl border border-stone-300 text-sm"
                            />
                            <input
                              value={editingOrder.boq_scope_note}
                              onChange={(e) =>
                                setEditingOrder({
                                  ...editingOrder,
                                  boq_scope_note: e.target.value,
                                })
                              }
                              placeholder="BOQ / Site Sizing Note"
                              className="min-h-[42px] px-3 rounded-xl border border-stone-300 text-sm"
                            />
                          </div>
                          <input
                            value={editingOrder.rough_scope}
                            onChange={(e) =>
                              setEditingOrder({
                                ...editingOrder,
                                rough_scope: e.target.value,
                              })
                            }
                            placeholder="Rough Scope"
                            className="w-full min-h-[42px] px-3 rounded-xl border border-stone-300 text-sm"
                          />
                          <div className="flex justify-end gap-2">
                            <button
                              type="button"
                              onClick={() => setEditingOrder(null)}
                              className="min-h-[40px] px-3.5 rounded-xl border border-stone-300 text-xs font-semibold text-stone-700"
                            >
                              Cancel
                            </button>
                            <button
                              type="submit"
                              disabled={busyId === order.id}
                              className="min-h-[40px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold"
                            >
                              {busyId === order.id ? "Saving..." : "Save Order Details"}
                            </button>
                          </div>
                        </form>
                      )}

                      {timelines[order.id] && (
                        <div className="mt-2 bg-white rounded-xl border border-stone-200 p-3 space-y-2 max-h-56 overflow-y-auto">
                          {timelines[order.id].map((ev) => (
                            <div
                              key={ev.id}
                              className={`text-xs flex items-start justify-between gap-2 ${
                                ev.is_undone ? "line-through text-stone-400" : "text-stone-700"
                              }`}
                            >
                              <div>
                                <b>{ev.track.toUpperCase()}</b>
                                {ev.item_name ? ` (${ev.item_name})` : ""}:{" "}
                                {ev.from_stage || "start"} → <b>{ev.to_stage}</b>
                                {ev.assigned_names?.length
                                  ? ` [${ev.assigned_names.join(", ")}]`
                                  : ""}
                                <span className="text-stone-400">
                                  {" "}
                                  • by {ev.actor_name}
                                </span>
                              </div>
                              <span className="text-stone-400 shrink-0">
                                {formatDate(ev.created_at)}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
