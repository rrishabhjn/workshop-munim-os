import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronRight, Plus, Search, X } from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import OrderDetailDrawer from "./OrderDetailDrawer";

const PRE_PROD_CODES = new Set([
  "lead",
  "design",
  "estimating",
  "quoted_awaiting_advance",
  "site_sizing_boq",
]);

const PAYMENT_STAGE_PILL_CLASS = {
  advance_pending: "bg-stone-100 text-stone-700 border-stone-200",
  advance_in: "bg-sky-50 text-sky-800 border-sky-200",
  partial_paid: "bg-amber-50 text-amber-900 border-amber-200",
  payment_complete: "bg-emerald-50 text-emerald-800 border-emerald-200",
};

const PAYMENT_STAGE_LABELS = {
  advance_pending: "Advance Pending",
  advance_in: "Advance In",
  partial_paid: "Partial Paid",
  payment_complete: "Complete ✓",
};

function orderNeedsMaterial(order) {
  const mats = order.item_materials || [];
  if (mats.length > 0) {
    return mats.some((m) => m.status === "needed");
  }
  return order.material_stage === "check_stock";
}

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
  const [searchQuery, setSearchQuery] = useState("");
  const [showNewLead, setShowNewLead] = useState(false);
  const [fullName, setFullName] = useState("");
  const [areaLandmark, setAreaLandmark] = useState("");
  const [roughScope, setRoughScope] = useState("");
  const [phone, setPhone] = useState("");
  const [busyNew, setBusyNew] = useState(false);

  const peopleById = useMemo(
    () => Object.fromEntries((people || []).map((p) => [p.id, p])),
    [people]
  );

  const pipelineStages = lookups?.pipeline_stages || [];
  const materialStages = lookups?.material_stages || [];

  useEffect(() => {
    if (!selectedOrderId) return;
    const target = (orders || []).find((o) => o.id === selectedOrderId);
    if (!target) return;
    const isVisible =
      filter === "pre_prod"
        ? PRE_PROD_CODES.has(target.pipeline_stage)
        : filter === "execution"
        ? target.pipeline_stage === "execution"
        : filter === "needs_material"
        ? target.status === "active" && orderNeedsMaterial(target)
        : filter === "packed_gate"
        ? (target.items || []).some((it) => it.stage === "packed_ready")
        : filter === "completed"
        ? target.pipeline_stage === "completed"
        : target.status === "active";
    if (!isVisible) {
      setFilter(target.pipeline_stage === "completed" ? "completed" : "active_all");
    }
  }, [selectedOrderId]);

  const filterCounts = useMemo(() => {
    const all = orders || [];
    return {
      active_all: all.filter((o) => o.status === "active").length,
      pre_prod: all.filter((o) => PRE_PROD_CODES.has(o.pipeline_stage)).length,
      execution: all.filter((o) => o.pipeline_stage === "execution").length,
      needs_material: all.filter(
        (o) => o.status === "active" && orderNeedsMaterial(o)
      ).length,
      packed_gate: all.filter((o) =>
        (o.items || []).some((it) => it.stage === "packed_ready")
      ).length,
      completed: all.filter((o) => o.pipeline_stage === "completed").length,
    };
  }, [orders]);

  const filteredOrders = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return (orders || []).filter((o) => {
      const stageMatch =
        filter === "pre_prod"
          ? PRE_PROD_CODES.has(o.pipeline_stage)
          : filter === "execution"
          ? o.pipeline_stage === "execution"
          : filter === "needs_material"
          ? o.status === "active" && orderNeedsMaterial(o)
          : filter === "packed_gate"
          ? (o.items || []).some((it) => it.stage === "packed_ready")
          : filter === "completed"
          ? o.pipeline_stage === "completed"
          : o.status === "active";
      if (!stageMatch) return false;
      if (!q) return true;

      if (
        (o.customer_name || "").toLowerCase().includes(q) ||
        (o.project_code || "").toLowerCase().includes(q) ||
        (o.site_area_landmark || "").toLowerCase().includes(q) ||
        (o.rough_scope || "").toLowerCase().includes(q)
      ) {
        return true;
      }
      const itemHit = (o.items || []).some((it) => {
        const shop = peopleById[it.production_mistri_id]?.full_name || "";
        const site = peopleById[it.assembly_mistri_id]?.full_name || "";
        return (
          (it.item_name || "").toLowerCase().includes(q) ||
          (it.assigned_karigar || "").toLowerCase().includes(q) ||
          shop.toLowerCase().includes(q) ||
          site.toLowerCase().includes(q)
        );
      });
      if (itemHit) return true;
      return (o.item_materials || []).some((m) =>
        (m.material_name || "").toLowerCase().includes(q)
      );
    });
  }, [orders, filter, searchQuery, peopleById]);

  const selectedOrder = useMemo(
    () => (orders || []).find((o) => o.id === selectedOrderId) || null,
    [orders, selectedOrderId]
  );

  async function handleCreateLead(e) {
    e.preventDefault();
    if (!fullName.trim() || !areaLandmark.trim() || !roughScope.trim()) return;
    setBusyNew(true);
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
      setBusyNew(false);
    }
  }

  return (
    <div className="space-y-4 pb-28">
      {/* Instant Search + Filter Pills + New Lead */}
      <div className="space-y-2.5">
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="w-4 h-4 text-stone-400 absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" />
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search customer, city, sofa, Mistri, material..."
              className="w-full min-h-[44px] pl-10 pr-9 rounded-xl border border-stone-200 bg-white text-sm outline-none focus:border-amber-500 shadow-xs"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery("")}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1 text-stone-400 hover:text-stone-700"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>

          <button
            type="button"
            onClick={() => setShowNewLead((v) => !v)}
            className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold flex items-center gap-1.5 shadow-sm shrink-0"
          >
            <Plus className="w-4 h-4" />
            <span>New Lead</span>
          </button>
        </div>

        <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
          {[
            { id: "active_all", label: `All Active (${filterCounts.active_all})` },
            { id: "pre_prod", label: `Pre-Prod (${filterCounts.pre_prod})` },
            { id: "execution", label: `In Execution (${filterCounts.execution})` },
            {
              id: "needs_material",
              label: `🪵 Needs Material (${filterCounts.needs_material})`,
            },
            {
              id: "packed_gate",
              label: `Packed Gate (${filterCounts.packed_gate})`,
            },
            { id: "completed", label: `Completed (${filterCounts.completed})` },
          ].map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setFilter(f.id)}
              className={`min-h-[38px] px-3.5 py-1 rounded-full text-xs font-semibold whitespace-nowrap transition ${
                filter === f.id
                  ? "bg-amber-900 text-white shadow-sm"
                  : "bg-white text-stone-700 border border-stone-200 hover:bg-stone-50"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
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
            Project code is generated automatically. Quote ₹ &amp; target delivery
            date can be added later once finalised.
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
              disabled={busyNew}
              className="min-h-[44px] px-5 rounded-xl bg-amber-900 text-white text-xs font-bold"
            >
              {busyNew ? "Saving..." : "Save Lead"}
            </button>
          </div>
        </form>
      )}

      {/* 3-Column Order Tiles Grid */}
      {filteredOrders.length === 0 ? (
        <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center text-sm text-stone-500">
          No orders match this filter. Tap <b>+ New Lead</b> or tell the AI bar{" "}
          <i>&ldquo;Manoj Sharma Ajmer Road ka naya lead daal do, modular kitchen&rdquo;</i>.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5">
          {filteredOrders.map((order) => {
            const items = order.items || [];
            const mats = order.item_materials || [];
            const isSelected = selectedOrderId === order.id;

            const inShopCount = items.filter((i) => i.stage === "in_shop").length;
            const packedCount = items.filter((i) => i.stage === "packed_ready").length;
            const atSiteCount = items.filter((i) => i.stage === "at_site").length;
            const qcDoneCount = items.filter((i) => i.stage === "qc_done").length;

            const needMatCount = mats.filter((m) => m.status === "needed").length;
            const ordMatCount = mats.filter((m) => m.status === "ordered").length;
            const shopMatCount = mats.filter((m) => m.status === "in_shop").length;

            const showPaymentGateAlert =
              packedCount > 0 && order.payment_stage !== "payment_complete";

            const stageLabel =
              pipelineStages.find((s) => s.code === order.pipeline_stage)?.label ||
              order.pipeline_stage;
            const matTrackLabel =
              materialStages.find((m) => m.code === order.material_stage)?.label ||
              order.material_stage;

            return (
              <div
                key={order.id}
                id={`order-card-${order.id}`}
                onClick={() => onSelectOrder(order.id)}
                className={`bg-white rounded-2xl border p-4 cursor-pointer transition flex flex-col justify-between gap-3 hover:shadow-md ${
                  isSelected
                    ? "border-amber-500 ring-2 ring-amber-200 shadow-md"
                    : "border-stone-200 shadow-xs"
                }`}
              >
                <div className="space-y-2.5">
                  {/* Header: Code + Deal Value */}
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-mono font-bold text-amber-950 bg-amber-100 px-2 py-0.5 rounded">
                      #{order.project_code}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {Number(order.quoted_total) > 0 ? (
                        <span className="text-[11px] font-bold text-stone-700 bg-stone-100 px-2 py-0.5 rounded-md">
                          {formatINR(order.paid_cache_inr)} /{" "}
                          {formatINR(order.quoted_total)}
                        </span>
                      ) : Number(order.paid_cache_inr) > 0 ? (
                        <span className="text-[11px] font-bold text-sky-900 bg-sky-50 border border-sky-200 px-2 py-0.5 rounded-md">
                          {formatINR(order.paid_cache_inr)} received
                        </span>
                      ) : null}
                      <ChevronRight className="w-4 h-4 text-stone-400" />
                    </div>
                  </div>

                  {/* Customer + Locality + Scope */}
                  <div className="space-y-0.5">
                    <h3 className="font-bold text-base text-stone-900 leading-snug">
                      {order.customer_name}
                    </h3>
                    {order.site_area_landmark &&
                      !(order.customer_name || "")
                        .toLowerCase()
                        .includes(order.site_area_landmark.toLowerCase()) && (
                        <div className="text-[11px] font-medium text-stone-500">
                          {order.site_area_landmark}
                        </div>
                      )}
                    {order.rough_scope && (
                      <p className="text-xs text-stone-600 line-clamp-2 pt-0.5">
                        {order.rough_scope}
                      </p>
                    )}
                  </div>

                  {/* Stage & Payment Pills */}
                  <div className="flex items-center gap-1.5 flex-wrap text-[11px]">
                    <span className="font-semibold px-2 py-0.5 rounded-full bg-amber-50 text-amber-950 border border-amber-200">
                      {stageLabel}
                    </span>
                    <span
                      className={`font-semibold px-2 py-0.5 rounded-full border ${
                        PAYMENT_STAGE_PILL_CLASS[order.payment_stage] ||
                        "bg-stone-100 text-stone-700 border-stone-200"
                      }`}
                    >
                      Pay:{" "}
                      {PAYMENT_STAGE_LABELS[order.payment_stage] ||
                        order.payment_stage.replace(/_/g, " ")}
                    </span>
                    {order.promised_delivery_date && (
                      <span className="text-stone-500 font-medium">
                        Due {formatDate(order.promised_delivery_date)}
                      </span>
                    )}
                  </div>
                </div>

                {/* Bottom Status Strip: Item Stages + Material Status Side-by-Side */}
                <div className="space-y-2 pt-2.5 border-t border-stone-100">
                  <div className="flex items-center gap-1.5 flex-wrap text-[11px]">
                    {items.length === 0 ? (
                      <span className="px-2 py-0.5 rounded-md bg-stone-50 text-stone-400 border border-stone-200">
                        No items yet
                      </span>
                    ) : (
                      <>
                        {inShopCount > 0 && (
                          <span className="font-semibold px-2 py-0.5 rounded-md bg-stone-100 text-stone-800">
                            In Shop: {inShopCount}
                          </span>
                        )}
                        {packedCount > 0 && (
                          <span className="font-semibold px-2 py-0.5 rounded-md bg-amber-100 text-amber-950">
                            Packed: {packedCount}
                          </span>
                        )}
                        {atSiteCount > 0 && (
                          <span className="font-semibold px-2 py-0.5 rounded-md bg-sky-100 text-sky-900">
                            At Site: {atSiteCount}
                          </span>
                        )}
                        {qcDoneCount > 0 && (
                          <span className="font-semibold px-2 py-0.5 rounded-md bg-emerald-100 text-emerald-900">
                            QC Done: {qcDoneCount}
                          </span>
                        )}
                      </>
                    )}

                    {/* Material Status Pill */}
                    {mats.length > 0 ? (
                      needMatCount > 0 ? (
                        <span className="font-bold px-2 py-0.5 rounded-md bg-amber-100 text-amber-950 border border-amber-200">
                          🪵 {needMatCount} To Order
                        </span>
                      ) : ordMatCount > 0 ? (
                        <span className="font-bold px-2 py-0.5 rounded-md bg-sky-50 text-sky-900 border border-sky-200">
                          🪵 {ordMatCount} Ordered
                        </span>
                      ) : (
                        <span className="font-bold px-2 py-0.5 rounded-md bg-emerald-50 text-emerald-800 border border-emerald-200">
                          🪵 {shopMatCount} In Shop
                        </span>
                      )
                    ) : (
                      order.material_stage !== "not_started" && (
                        <span
                          className={`font-semibold px-2 py-0.5 rounded-md border ${
                            order.material_stage === "all_in_shop"
                              ? "bg-emerald-50 text-emerald-800 border-emerald-200"
                              : "bg-sky-50 text-sky-900 border-sky-200"
                          }`}
                        >
                          🪵 {matTrackLabel}
                        </span>
                      )
                    )}
                  </div>

                  {showPaymentGateAlert && (
                    <div className="flex items-center gap-1.5 text-[11px] font-semibold text-amber-950 bg-amber-50 border border-amber-300 rounded-lg px-2.5 py-1.5">
                      <AlertTriangle className="w-3.5 h-3.5 text-amber-700 shrink-0" />
                      <span>Pre-Dispatch Gate: Packed while payment pending</span>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Slide-Over Order Workspace Drawer */}
      {selectedOrder && (
        <OrderDetailDrawer
          order={selectedOrder}
          lookups={lookups}
          people={people}
          onClose={() => onSelectOrder(null)}
          onMutated={onMutated}
          onShowUndoToast={onShowUndoToast}
        />
      )}
    </div>
  );
}
