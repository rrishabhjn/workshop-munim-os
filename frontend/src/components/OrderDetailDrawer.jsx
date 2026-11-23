import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Archive,
  Camera,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  FileText,
  History,
  Image,
  Pencil,
  Plus,
  Share2,
  Trash2,
  X,
} from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import { matchStockSkuForMaterial } from "../lib/inventoryMatch";
import { shareKarigarJobCard } from "../lib/jobCard";

const MATERIAL_UNITS = [
  "sheets",
  "pcs",
  "mtr",
  "sqft",
  "rft",
  "kg",
  "ltr",
  "box",
  "set",
  "roll",
];

const MATERIAL_STATUS_OPTIONS = [
  { code: "needed", label: "To Order" },
  { code: "ordered", label: "Ordered" },
  { code: "in_shop", label: "In Shop" },
];

const ITEM_STAGE_PILL_CLASS = {
  in_shop: "bg-stone-100 text-stone-800 border-stone-300",
  packed_ready: "bg-amber-100 text-amber-950 border-amber-300",
  at_site: "bg-sky-100 text-sky-900 border-sky-300",
  qc_done: "bg-emerald-100 text-emerald-900 border-emerald-300",
};

const MATERIAL_STATUS_PILL_CLASS = {
  needed: "bg-amber-100 text-amber-950 border-amber-300",
  ordered: "bg-sky-100 text-sky-900 border-sky-300",
  in_shop: "bg-emerald-100 text-emerald-900 border-emerald-300",
  mixed: "bg-stone-100 text-stone-700 border-stone-300",
};

const PAYMENT_STAGE_PILL_CLASS = {
  advance_pending: "bg-stone-100 text-stone-700 border-stone-200",
  advance_in: "bg-sky-50 text-sky-800 border-sky-200",
  partial_paid: "bg-amber-50 text-amber-900 border-amber-200",
  payment_complete: "bg-emerald-50 text-emerald-800 border-emerald-200",
};

const DEFAULT_PAYMENT_STAGES = [
  { code: "advance_pending", label: "Advance Pending" },
  { code: "advance_in", label: "Advance In" },
  { code: "partial_paid", label: "Partial Paid" },
  { code: "payment_complete", label: "Complete ✓" },
];

function fmtQty(val) {
  const n = Number(val || 0);
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)));
}

export default function OrderDetailDrawer({
  order,
  lookups,
  people,
  inventoryItems = [],
  onClose,
  onMutated,
  onShowUndoToast,
}) {
  const [activeTab, setActiveTab] = useState("items");
  const [expandedItemId, setExpandedItemId] = useState(null);
  const [editingMistriItemId, setEditingMistriItemId] = useState(null);
  const [itemSpecDraft, setItemSpecDraft] = useState({});
  const [matFilter, setMatFilter] = useState("all");
  const [copiedMsg, setCopiedMsg] = useState(false);

  const [newMatForm, setNewMatForm] = useState({});
  const [orderWideMat, setOrderWideMat] = useState({
    material_name: "",
    quantity: "1",
    unit: "sheets",
  });
  const [newItemName, setNewItemName] = useState("");
  const [newItemMistri, setNewItemMistri] = useState("");

  const [showOrderControls, setShowOrderControls] = useState(false);
  const [editingOrder, setEditingOrder] = useState(null);
  const [timeline, setTimeline] = useState(null);
  const [busyKey, setBusyKey] = useState(null);
  const [uploadErr, setUploadErr] = useState("");
  const orderAttachRef = useRef(null);
  const itemPhotoRef = useRef(null);
  const photoUploadItemIdRef = useRef(null);

  useEffect(() => {
    setExpandedItemId(null);
    setEditingMistriItemId(null);
    setEditingOrder(null);
    setTimeline(null);
    setCopiedMsg(false);
  }, [order?.id]);

  const peopleById = useMemo(
    () => Object.fromEntries((people || []).map((p) => [p.id, p])),
    [people]
  );
  const operators = useMemo(
    () => (people || []).filter((p) => p.person_type === "operator"),
    [people]
  );
  const mistris = useMemo(
    () => (people || []).filter((p) => p.person_type !== "operator"),
    [people]
  );
  const allAssignees = useMemo(
    () => [...mistris, ...operators],
    [mistris, operators]
  );

  const pipelineStages = lookups?.pipeline_stages || [];
  const materialStages = lookups?.material_stages || [];
  const itemStages = lookups?.item_stages || [];
  const materialCategories = lookups?.material_categories || [
    { code: "ply_wood", label: "Ply / Wood" },
    { code: "laminate", label: "Laminate / Sunmica" },
    { code: "hardware", label: "Hardware" },
    { code: "foam_fabric", label: "Foam / Fabric" },
    { code: "polish", label: "Polish / Adhesive" },
  ];
  const catLabelMap = useMemo(
    () => Object.fromEntries(materialCategories.map((c) => [c.code, c.label])),
    [materialCategories]
  );

  const items = order?.items || [];
  const allMaterials = order?.item_materials || [];
  const itemNameById = Object.fromEntries(items.map((it) => [it.id, it.item_name]));
  const toOrderTotal = allMaterials.filter((m) => m.status === "needed").length;

  // Build customer-level aggregated material groups across items
  const groupedByCategory = useMemo(() => {
    const filtered = allMaterials.filter((m) =>
      matFilter === "all" ? true : m.status === matFilter
    );
    const aggMap = new Map();
    for (const m of filtered) {
      const cat = m.category || "ply_wood";
      const unit = (m.unit || "pcs").trim();
      const key = `${cat}::${(m.material_name || "").trim().toLowerCase()}::${unit.toLowerCase()}`;
      if (!aggMap.has(key)) {
        aggMap.set(key, {
          key,
          category: cat,
          materialName: (m.material_name || "").trim(),
          unit,
          totalQty: 0,
          neededQty: 0,
          firstNeededRow: null,
          statuses: new Set(),
          materialIds: [],
          breakdown: [],
        });
      }
      const entry = aggMap.get(key);
      const q = Number(m.quantity || 0);
      entry.totalQty = Number((entry.totalQty + q).toFixed(2));
      if ((m.status || "needed") === "needed") {
        entry.neededQty = Number((entry.neededQty + q).toFixed(2));
        if (!entry.firstNeededRow) entry.firstNeededRow = m;
      }
      entry.statuses.add(m.status || "needed");
      entry.materialIds.push(m.id);
      const iName = itemNameById[m.item_id] || "Order-Wide";
      const existingBr = entry.breakdown.find((b) => b.itemName === iName);
      if (existingBr) {
        existingBr.qty = Number((existingBr.qty + q).toFixed(2));
      } else {
        entry.breakdown.push({ itemName: iName, qty: q });
      }
    }

    const groups = [];
    for (const cat of materialCategories) {
      const rows = Array.from(aggMap.values()).filter(
        (g) => g.category === cat.code
      );
      if (rows.length > 0) {
        groups.push({ category: cat, rows });
      }
    }
    return groups;
  }, [allMaterials, matFilter, materialCategories, order?.id]);

  if (!order) return null;

  const attachmentPaths = Array.isArray(order.metadata?.uploaded_gcs_paths)
    ? order.metadata.uploaded_gcs_paths.filter(Boolean)
    : [];

  async function handleOpenAttachment(path) {
    setBusyKey(`att:${path}`);
    try {
      const res = await apiFetch(
        `/api/media/signed-url?path=${encodeURIComponent(path)}`
      );
      if (res?.url) {
        window.open(res.url, "_blank", "noopener,noreferrer");
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleOrderAttachmentFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !order?.id) return;
    setUploadErr("");
    setBusyKey("att_upload");
    try {
      const fd = new FormData();
      fd.append("file", file);
      await apiFetch(`/api/production/orders/${order.id}/attachments`, {
        method: "POST",
        body: fd,
      });
      await onMutated();
    } catch (err) {
      setUploadErr(err?.message || "Attachment upload failed.");
    } finally {
      setBusyKey(null);
    }
  }

  async function handleItemPhotoFile(e) {
    const file = e.target.files?.[0];
    const targetItemId = photoUploadItemIdRef.current;
    e.target.value = "";
    photoUploadItemIdRef.current = null;
    if (!file || !targetItemId) return;
    setUploadErr("");
    setBusyKey(`item_photo:${targetItemId}`);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await apiFetch(`/api/production/items/${targetItemId}/photo`, {
        method: "POST",
        body: fd,
      });
      await onMutated();
    } catch (err) {
      setUploadErr(err?.message || "Item photo upload failed.");
    } finally {
      setBusyKey(null);
    }
  }

  async function handlePipelineChange(toStage, extra = {}) {
    setBusyKey("order");
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
      setBusyKey(null);
    }
  }

  async function handleMaterialTrackChange(toStage) {
    setBusyKey("order");
    try {
      const res = await apiFetch(`/api/production/orders/${order.id}/material`, {
        method: "POST",
        body: JSON.stringify({ to_stage: toStage }),
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Material track updated", res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleToggleScopeChanged() {
    await apiFetch(`/api/production/orders/${order.id}`, {
      method: "PATCH",
      body: JSON.stringify({ boq_scope_changed: !order.boq_scope_changed }),
    });
    await onMutated();
  }

  function startEditOrderDetails() {
    if (editingOrder) {
      setEditingOrder(null);
      return;
    }
    setEditingOrder({
      quoted_total: order.quoted_total ?? 0,
      paid_cache_inr: order.paid_cache_inr ?? 0,
      payment_stage: order.payment_stage || "advance_pending",
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
    setBusyKey("order-edit");
    try {
      await apiFetch(`/api/production/orders/${order.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          quoted_total: Number(editingOrder.quoted_total || 0),
          paid_cache_inr: Number(editingOrder.paid_cache_inr || 0),
          payment_stage: editingOrder.payment_stage,
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
      setBusyKey(null);
    }
  }

  async function toggleTimeline() {
    if (timeline) {
      setTimeline(null);
      return;
    }
    const rows = await apiFetch(`/api/production/orders/${order.id}/timeline`);
    setTimeline(rows);
  }

  async function handleAddItem(e) {
    e.preventDefault();
    const name = newItemName.trim();
    if (!name) return;
    setBusyKey("add-item");
    try {
      const res = await apiFetch(`/api/production/orders/${order.id}/items`, {
        method: "POST",
        body: JSON.stringify({
          item_name: name,
          to_stage: "in_shop",
          production_mistri_id: newItemMistri || null,
        }),
      });
      setNewItemName("");
      setNewItemMistri("");
      await onMutated();
      if (res?.item_id) setExpandedItemId(res.item_id);
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || `Added ${name}`, res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleItemPatch(itemId, patch) {
    setBusyKey(itemId);
    try {
      const res = await apiFetch(`/api/production/items/${itemId}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Item updated", res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleAddMaterial(e, itemId) {
    e.preventDefault();
    const draft = newMatForm[itemId] || {};
    const mName = (draft.material_name || "").trim();
    if (!mName) return;
    setBusyKey(`mat-${itemId}`);
    try {
      const res = await apiFetch(`/api/production/items/${itemId}/materials`, {
        method: "POST",
        body: JSON.stringify({
          material_name: mName,
          quantity: Math.max(Number(draft.quantity || 1), 0.01),
          unit: draft.unit || "sheets",
          status: "needed",
        }),
      });
      setNewMatForm((prev) => ({
        ...prev,
        [itemId]: { material_name: "", quantity: "1", unit: draft.unit || "sheets" },
      }));
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || `Added ${mName}`, res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleMaterialStatusPatch(materialId, nextStatus) {
    setBusyKey(materialId);
    try {
      const res = await apiFetch(`/api/production/materials/${materialId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: nextStatus }),
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Material updated", res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleDeleteMaterial(materialId) {
    setBusyKey(materialId);
    try {
      const res = await apiFetch(`/api/production/materials/${materialId}`, {
        method: "DELETE",
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Material removed", res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleBulkStatus(materialIds, nextStatus) {
    if (!materialIds?.length) return;
    setBusyKey(`bulk-${materialIds[0]}`);
    try {
      const res = await apiFetch(
        `/api/production/orders/${order.id}/materials/bulk-status`,
        {
          method: "POST",
          body: JSON.stringify({ material_ids: materialIds, status: nextStatus }),
        }
      );
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || "Updated material status", res.undo_event_ids);
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleAddOrderWideMaterial(e) {
    e.preventDefault();
    const mName = (orderWideMat.material_name || "").trim();
    if (!mName) return;
    setUploadErr("");
    setBusyKey("mat-order-wide");
    try {
      const res = await apiFetch(`/api/production/orders/${order.id}/materials`, {
        method: "POST",
        body: JSON.stringify({
          material_name: mName,
          quantity: Math.max(Number(orderWideMat.quantity || 1), 0.01),
          unit: orderWideMat.unit || "sheets",
          status: "needed",
        }),
      });
      setOrderWideMat((prev) => ({ ...prev, material_name: "", quantity: "1" }));
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || `Added ${mName}`, res.undo_event_ids);
      }
    } catch (err) {
      let msg = err?.message || "Failed adding order material";
      if (msg.startsWith("{")) {
        try {
          msg = JSON.parse(msg)?.detail || msg;
        } catch {
          // keep raw message
        }
      }
      setUploadErr(msg);
    } finally {
      setBusyKey(null);
    }
  }

  async function handlePullStockForMaterial(sku, { projectId, itemId, itemMaterialId, pullQty }) {
    setUploadErr("");
    setBusyKey(`pull-${itemMaterialId || sku.id}`);
    try {
      const res = await apiFetch("/api/inventory/attribute", {
        method: "POST",
        body: JSON.stringify({
          inventory_item_id: sku.id,
          project_id: projectId,
          item_id: itemId || null,
          item_material_id: itemMaterialId || null,
          quantity: pullQty,
        }),
      });
      await onMutated();
      if (res?.undo_event_ids?.length) {
        onShowUndoToast(res.receipt || `Pulled ${sku.name}`, res.undo_event_ids);
      }
    } catch (err) {
      let msg = err?.message || "Failed pulling stock for order";
      if (msg.startsWith("{")) {
        try {
          msg = JSON.parse(msg)?.detail || msg;
        } catch {
          // keep raw message
        }
      }
      setUploadErr(msg);
    } finally {
      setBusyKey(null);
    }
  }

  async function handleCopyToOrderWhatsApp() {
    const neededMats = allMaterials.filter((m) => m.status === "needed");
    const sourceList = neededMats.length > 0 ? neededMats : allMaterials;
    if (!sourceList.length) return;

    const agg = new Map();
    for (const m of sourceList) {
      const cat = m.category || "ply_wood";
      const unit = (m.unit || "pcs").trim();
      const k = `${cat}::${(m.material_name || "").trim().toLowerCase()}::${unit.toLowerCase()}`;
      if (!agg.has(k)) {
        agg.set(k, {
          category: cat,
          name: (m.material_name || "").trim(),
          unit,
          qty: 0,
          items: new Map(),
        });
      }
      const row = agg.get(k);
      const q = Number(m.quantity || 0);
      row.qty = Number((row.qty + q).toFixed(2));
      const iName = itemNameById[m.item_id] || "Order-Wide";
      row.items.set(iName, Number(((row.items.get(iName) || 0) + q).toFixed(2)));
    }

    const lines = [
      `*${order.customer_name} (#${order.project_code}) — ${
        neededMats.length > 0 ? "To Order Material List" : "Full Material List"
      }*`,
    ];
    for (const cat of materialCategories) {
      const rows = Array.from(agg.values()).filter((r) => r.category === cat.code);
      if (!rows.length) continue;
      lines.push(`\n*${cat.label}:*`);
      for (const r of rows) {
        const parts = Array.from(r.items.entries())
          .map(([iName, q]) => `${iName}: ${fmtQty(q)}`)
          .join(", ");
        lines.push(`• ${r.name} — ${fmtQty(r.qty)} ${r.unit} (${parts})`);
      }
    }
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopiedMsg(true);
      setTimeout(() => setCopiedMsg(false), 2200);
    } catch {
      setCopiedMsg(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      {/* Backdrop */}
      <div
        onClick={onClose}
        className="fixed inset-0 bg-black/30 backdrop-blur-[1px]"
      />

      {/* Slide-Over Panel */}
      <div className="relative z-10 w-full max-w-2xl bg-stone-50 h-full flex flex-col shadow-2xl border-l border-stone-200">
        {/* Sticky Drawer Header */}
        <div className="bg-white border-b border-stone-200 p-4 space-y-3 shrink-0">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 space-y-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-mono font-bold text-amber-950 bg-amber-100 px-2 py-0.5 rounded">
                  #{order.project_code}
                </span>
                <h2 className="font-extrabold text-lg text-stone-900 truncate">
                  {order.customer_name}
                </h2>
                {Number(order.quoted_total) > 0 ? (
                  <span className="text-xs font-bold text-stone-700 bg-stone-100 px-2.5 py-0.5 rounded-lg">
                    {formatINR(order.paid_cache_inr)} / {formatINR(order.quoted_total)}
                  </span>
                ) : Number(order.paid_cache_inr) > 0 ? (
                  <span className="text-xs font-bold text-sky-900 bg-sky-50 border border-sky-200 px-2.5 py-0.5 rounded-lg">
                    {formatINR(order.paid_cache_inr)} received
                  </span>
                ) : null}
                <span
                  className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${
                    PAYMENT_STAGE_PILL_CLASS[order.payment_stage] ||
                    "bg-stone-100 text-stone-700 border-stone-200"
                  }`}
                >
                  Pay:{" "}
                  {DEFAULT_PAYMENT_STAGES.find((s) => s.code === order.payment_stage)?.label ||
                    (order.payment_stage || "advance_pending").replace(/_/g, " ")}
                </span>
              </div>
              <p className="text-xs text-stone-600 line-clamp-2">
                {order.rough_scope || order.site_area_landmark || "Customer Order"}
              </p>
            </div>

            <button
              type="button"
              onClick={onClose}
              aria-label="Close order drawer"
              className="min-h-[44px] min-w-[44px] rounded-xl border border-stone-200 bg-white flex items-center justify-center text-stone-600 hover:bg-stone-100 shrink-0"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Quick Action Strip & Collapsible Pipeline Controls */}
          <div className="flex items-center justify-between gap-2 flex-wrap text-xs">
            <div className="flex items-center gap-1.5 flex-wrap">
              <button
                type="button"
                onClick={() => setShowOrderControls((v) => !v)}
                className="min-h-[36px] px-2.5 rounded-lg border border-stone-200 bg-stone-50 font-semibold text-stone-700 flex items-center gap-1"
              >
                <span>
                  Stage:{" "}
                  {pipelineStages.find((s) => s.code === order.pipeline_stage)?.label ||
                    order.pipeline_stage}
                </span>
                {showOrderControls ? (
                  <ChevronUp className="w-3.5 h-3.5" />
                ) : (
                  <ChevronDown className="w-3.5 h-3.5" />
                )}
              </button>

              <button
                type="button"
                onClick={startEditOrderDetails}
                className="min-h-[36px] px-2.5 rounded-lg border border-stone-200 bg-white font-semibold text-stone-700 flex items-center gap-1"
              >
                <Pencil className="w-3.5 h-3.5" />
                <span>{editingOrder ? "Close Edit" : "Edit Order"}</span>
              </button>

              <button
                type="button"
                onClick={toggleTimeline}
                className="min-h-[36px] px-2.5 rounded-lg border border-stone-200 bg-white font-semibold text-stone-700 flex items-center gap-1"
              >
                <History className="w-3.5 h-3.5" />
                <span>History</span>
              </button>

              {attachmentPaths.map((p, idx) => {
                const isPdf = String(p).toLowerCase().endsWith(".pdf");
                const label =
                  attachmentPaths.length > 1
                    ? `${isPdf ? "Spec PDF" : "File"} ${idx + 1}`
                    : isPdf
                    ? "Spec PDF"
                    : "Attachment";
                return (
                  <button
                    key={p}
                    type="button"
                    disabled={busyKey === `att:${p}`}
                    onClick={() => handleOpenAttachment(p)}
                    className="min-h-[36px] px-2.5 rounded-lg border border-amber-200 bg-amber-50 font-semibold text-amber-950 flex items-center gap-1"
                  >
                    <FileText className="w-3.5 h-3.5" />
                    <span>{label}</span>
                  </button>
                );
              })}

              <input
                ref={orderAttachRef}
                type="file"
                accept="image/*,application/pdf"
                onChange={handleOrderAttachmentFile}
                className="hidden"
              />
              <input
                ref={itemPhotoRef}
                type="file"
                accept="image/*"
                onChange={handleItemPhotoFile}
                className="hidden"
              />
              <button
                type="button"
                disabled={busyKey === "att_upload"}
                onClick={() => orderAttachRef.current?.click()}
                className="min-h-[36px] px-2.5 rounded-lg border border-stone-200 bg-white font-semibold text-stone-700 flex items-center gap-1"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>{busyKey === "att_upload" ? "Uploading..." : "Attach Spec / Sizing"}</span>
              </button>
            </div>

            {order.pipeline_stage !== "lost" && (
              <button
                type="button"
                onClick={() => handlePipelineChange("lost")}
                className="min-h-[36px] px-2.5 rounded-lg border border-rose-200 bg-rose-50 font-semibold text-rose-700 flex items-center gap-1"
              >
                <Archive className="w-3.5 h-3.5" />
                <span>Mark Lost</span>
              </button>
            )}
          </div>

          {uploadErr && (
            <p className="text-xs font-medium text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-2.5 py-1.5">
              {uploadErr}
            </p>
          )}

          {showOrderControls && (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-1 bg-stone-50 p-3 rounded-xl border border-stone-200">
              <div className="space-y-1">
                <label className="text-[11px] font-bold uppercase text-stone-500">
                  Pipeline Stage
                </label>
                <select
                  value={order.pipeline_stage}
                  disabled={busyKey === "order"}
                  onChange={(e) => handlePipelineChange(e.target.value)}
                  className="w-full min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white font-semibold"
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
                  Ansh Material Track
                </label>
                <select
                  value={order.material_stage}
                  disabled={busyKey === "order"}
                  onChange={(e) => handleMaterialTrackChange(e.target.value)}
                  className="w-full min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white font-semibold"
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
                    ? "Estimator"
                    : "Sizing & Scope"}
                </label>
                {order.pipeline_stage === "estimating" ? (
                  <select
                    value={(order.estimator_ids || [])[0] || ""}
                    onChange={(e) =>
                      handlePipelineChange("estimating", {
                        estimator_ids: e.target.value ? [e.target.value] : [],
                      })
                    }
                    className="w-full min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                  >
                    <option value="">Assign Estimator...</option>
                    {operators.map((op) => (
                      <option key={op.id} value={op.id}>
                        {op.full_name}
                      </option>
                    ))}
                  </select>
                ) : (
                  <div className="flex items-center gap-1.5">
                    <select
                      value={order.sizing_mistri_id || ""}
                      onChange={(e) =>
                        handlePipelineChange(order.pipeline_stage, {
                          sizing_mistri_id: e.target.value || null,
                        })
                      }
                      className="flex-1 min-h-[40px] px-2 rounded-lg border border-stone-300 text-xs bg-white"
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
                      onClick={handleToggleScopeChanged}
                      className={`min-h-[40px] px-2 rounded-lg border text-[11px] font-semibold shrink-0 ${
                        order.boq_scope_changed
                          ? "bg-amber-100 border-amber-400 text-amber-950"
                          : "bg-white border-stone-300 text-stone-600"
                      }`}
                    >
                      {order.boq_scope_changed ? "Scope ⚠️" : "Scope ✓"}
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}

          {editingOrder && (
            <form
              onSubmit={handleSaveOrderDetails}
              className="bg-amber-50/50 rounded-xl border border-amber-200 p-3 space-y-2"
            >
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <label className="space-y-0.5">
                  <span className="text-[10px] font-bold uppercase text-stone-500">
                    Deal Value ₹
                  </span>
                  <input
                    type="number"
                    value={editingOrder.quoted_total}
                    onChange={(e) =>
                      setEditingOrder({ ...editingOrder, quoted_total: e.target.value })
                    }
                    placeholder="Deal Value ₹"
                    className="w-full min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                  />
                </label>
                <label className="space-y-0.5">
                  <span className="text-[10px] font-bold uppercase text-stone-500">
                    Paid / Advance ₹
                  </span>
                  <input
                    type="number"
                    min="0"
                    value={editingOrder.paid_cache_inr}
                    onChange={(e) => {
                      const val = e.target.value;
                      const numPaid = Number(val || 0);
                      const numQuoted = Number(editingOrder.quoted_total || 0);
                      let nextStage = editingOrder.payment_stage;
                      if (numPaid <= 0) {
                        nextStage = "advance_pending";
                      } else if (numQuoted > 0 && numPaid >= numQuoted) {
                        nextStage = "payment_complete";
                      } else if (nextStage === "advance_pending") {
                        nextStage = "advance_in";
                      }
                      setEditingOrder({
                        ...editingOrder,
                        paid_cache_inr: val,
                        payment_stage: nextStage,
                      });
                    }}
                    placeholder="Paid / Advance ₹"
                    className="w-full min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                  />
                </label>
                <label className="space-y-0.5">
                  <span className="text-[10px] font-bold uppercase text-stone-500">
                    Payment Status
                  </span>
                  <select
                    value={editingOrder.payment_stage}
                    onChange={(e) =>
                      setEditingOrder({ ...editingOrder, payment_stage: e.target.value })
                    }
                    className="w-full min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white font-semibold"
                  >
                    {(lookups?.payment_stages?.length
                      ? lookups.payment_stages
                      : DEFAULT_PAYMENT_STAGES
                    ).map((s) => (
                      <option key={s.code} value={s.code}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <input
                  type="date"
                  value={editingOrder.promised_delivery_date}
                  onChange={(e) =>
                    setEditingOrder({
                      ...editingOrder,
                      promised_delivery_date: e.target.value,
                    })
                  }
                  className="min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                />
                <input
                  value={editingOrder.phone}
                  onChange={(e) =>
                    setEditingOrder({ ...editingOrder, phone: e.target.value })
                  }
                  placeholder="Phone"
                  className="min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
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
                  placeholder="Site Area / City"
                  className="min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                />
                <input
                  value={editingOrder.rough_scope}
                  onChange={(e) =>
                    setEditingOrder({ ...editingOrder, rough_scope: e.target.value })
                  }
                  placeholder="Rough Scope"
                  className="min-h-[40px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                />
              </div>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setEditingOrder(null)}
                  className="min-h-[36px] px-3 rounded-lg border border-stone-300 bg-white text-xs font-semibold"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={busyKey === "order-edit"}
                  className="min-h-[36px] px-4 rounded-lg bg-amber-900 text-white text-xs font-bold"
                >
                  {busyKey === "order-edit" ? "Saving..." : "Save Order"}
                </button>
              </div>
            </form>
          )}

          {timeline && (
            <div className="bg-stone-50 rounded-xl border border-stone-200 p-2.5 space-y-1.5 max-h-40 overflow-y-auto">
              {timeline.length === 0 ? (
                <div className="text-xs text-stone-400">No stage history yet.</div>
              ) : (
                timeline.map((ev) => (
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
                      <span className="text-stone-400"> • {ev.actor_name}</span>
                    </div>
                    <span className="text-stone-400 shrink-0">
                      {formatDate(ev.created_at)}
                    </span>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Segmented Tab Switcher: Furniture Items vs Customer Material Rollup */}
          <div className="grid grid-cols-2 gap-1.5 bg-stone-100 p-1 rounded-xl">
            <button
              type="button"
              onClick={() => setActiveTab("items")}
              className={`min-h-[40px] px-3 rounded-lg text-xs font-bold flex items-center justify-center gap-1.5 transition ${
                activeTab === "items"
                  ? "bg-white text-amber-950 shadow-sm"
                  : "text-stone-600 hover:text-stone-900"
              }`}
            >
              <span>🪑 Furniture Items ({items.length})</span>
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("materials")}
              className={`min-h-[40px] px-3 rounded-lg text-xs font-bold flex items-center justify-center gap-1.5 transition ${
                activeTab === "materials"
                  ? "bg-white text-amber-950 shadow-sm"
                  : "text-stone-600 hover:text-stone-900"
              }`}
            >
              <span>🪵 All Materials ({allMaterials.length})</span>
              {toOrderTotal > 0 && (
                <span className="px-1.5 py-0.5 rounded-full bg-amber-200 text-amber-950 text-[10px]">
                  {toOrderTotal} To Order
                </span>
              )}
            </button>
          </div>
        </div>

        {/* Scrollable Drawer Body */}
        <div className="flex-1 overflow-y-auto p-4 pb-36 space-y-3">
          {activeTab === "items" ? (
            <>
              {items.length === 0 ? (
                <div className="bg-white rounded-xl border border-stone-200 p-5 text-center text-xs text-stone-500">
                  No furniture items yet. Add items like <b>Modular Kitchen</b> or{" "}
                  <b>Master Bed</b> below to track shop stages &amp; raw materials.
                </div>
              ) : (
                <div className="space-y-2">
                  {items.map((it) => {
                    const isOpen = expandedItemId === it.id;
                    const itemMats = it.materials || [];
                    const matNeed = itemMats.filter((m) => m.status === "needed").length;
                    const shopName =
                      peopleById[it.production_mistri_id]?.full_name ||
                      it.assigned_karigar ||
                      "";
                    const siteName =
                      peopleById[it.assembly_mistri_id]?.full_name || "";
                    const matDraft = newMatForm[it.id] || {
                      material_name: "",
                      quantity: "1",
                      unit: "sheets",
                    };
                    const showMistriForm = editingMistriItemId === it.id;

                    return (
                      <div
                        key={it.id}
                        className="bg-white rounded-xl border border-stone-200 shadow-xs overflow-hidden"
                      >
                        {/* Compact 1-Line Item Header (No 4-Button Wall!) */}
                        <div
                          onClick={() =>
                            setExpandedItemId(isOpen ? null : it.id)
                          }
                          className="px-3.5 py-3 flex items-center justify-between gap-2 cursor-pointer hover:bg-stone-50/70 transition"
                        >
                          <div className="min-w-0 flex-1 space-y-0.5">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="font-bold text-sm text-stone-900">
                                {it.item_name}
                              </span>
                              {itemMats.length > 0 && (
                                <span
                                  className={`text-[11px] font-semibold px-2 py-0.5 rounded-full ${
                                    matNeed > 0
                                      ? "bg-amber-100 text-amber-950"
                                      : "bg-emerald-50 text-emerald-800"
                                  }`}
                                >
                                  🪵 {itemMats.length}
                                  {matNeed > 0 ? ` (${matNeed} To Order)` : " ✓"}
                                </span>
                              )}
                            </div>
                            <div className="text-[11px] text-stone-500 flex items-center gap-2 flex-wrap">
                              {shopName && <span>🔨 {shopName}</span>}
                              {siteName && <span>🏠 {siteName}</span>}
                              {it.dimensions && <span>• {it.dimensions}</span>}
                              {!shopName && !siteName && !it.dimensions && (
                                <span>Tap to add raw materials or Mistri</span>
                              )}
                            </div>
                          </div>

                          {/* Single Current-Status Pill Dropdown */}
                          <div
                            onClick={(e) => e.stopPropagation()}
                            className="flex items-center gap-1.5 shrink-0"
                          >
                            <select
                              value={it.stage}
                              disabled={busyKey === it.id}
                              onChange={(e) =>
                                handleItemPatch(it.id, { to_stage: e.target.value })
                              }
                              className={`min-h-[38px] px-2.5 rounded-lg border text-xs font-bold cursor-pointer ${
                                ITEM_STAGE_PILL_CLASS[it.stage] ||
                                ITEM_STAGE_PILL_CLASS.in_shop
                              }`}
                            >
                              {itemStages.map((st) => (
                                <option key={st.code} value={st.code}>
                                  {st.label}
                                </option>
                              ))}
                            </select>

                            <button
                              type="button"
                              onClick={() =>
                                setExpandedItemId(isOpen ? null : it.id)
                              }
                              className="p-1.5 rounded-lg text-stone-400 hover:bg-stone-100"
                            >
                              {isOpen ? (
                                <ChevronUp className="w-4 h-4" />
                              ) : (
                                <ChevronDown className="w-4 h-4" />
                              )}
                            </button>
                          </div>
                        </div>

                        {/* Expanded Accordion Body: Raw Materials + Mistri/Job Card */}
                        {isOpen && (
                          <div className="border-t border-stone-200 bg-stone-50/60 p-3.5 space-y-3">
                            {it.stage === "packed_ready" &&
                              order.payment_stage !== "payment_complete" && (
                                <div className="flex items-center gap-2 text-xs font-semibold text-amber-950 bg-amber-50 border border-amber-300 rounded-lg px-2.5 py-1.5">
                                  <AlertTriangle className="w-3.5 h-3.5 text-amber-700 shrink-0" />
                                  <span>
                                    Pre-Dispatch Gate: Packed while payment is{" "}
                                    {order.payment_stage.replace(/_/g, " ")}.
                                  </span>
                                </div>
                              )}

                            {it.specs_notes && (
                              <div className="bg-white rounded-lg border border-stone-200 px-2.5 py-2 text-xs text-stone-700 leading-relaxed">
                                <span className="font-bold text-stone-900">Specs: </span>
                                <span>{it.specs_notes}</span>
                              </div>
                            )}

                            {/* Item Raw Materials List */}
                            <div className="space-y-2">
                              <div className="text-[11px] font-bold uppercase tracking-wide text-stone-500">
                                Raw Material Requirements ({itemMats.length})
                              </div>

                              {itemMats.length > 0 && (
                                <div className="space-y-1.5">
                                  {itemMats.map((m) => {
                                    const stockSku =
                                      (m.status || "needed") === "needed"
                                        ? matchStockSkuForMaterial(inventoryItems, m)
                                        : null;
                                    return (
                                      <div
                                        key={m.id}
                                        className="bg-white rounded-lg border border-stone-200 px-2.5 py-2 flex flex-wrap sm:flex-nowrap items-center justify-between gap-2"
                                      >
                                        <div className="min-w-0 flex-1">
                                          <div className="flex items-center gap-1.5 flex-wrap">
                                            <span className="font-semibold text-xs text-stone-900">
                                              {m.material_name}
                                            </span>
                                            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-stone-100 text-stone-600">
                                              {catLabelMap[m.category] || m.category}
                                            </span>
                                          </div>
                                          <div className="text-xs text-stone-600 font-medium">
                                            {fmtQty(m.quantity)} {m.unit}
                                          </div>
                                        </div>

                                        <div className="flex items-center gap-1.5 flex-wrap shrink-0">
                                          {stockSku && (
                                            <button
                                              type="button"
                                              disabled={busyKey === `pull-${m.id}`}
                                              onClick={() =>
                                                handlePullStockForMaterial(stockSku, {
                                                  projectId: order.id,
                                                  itemId: m.item_id || it.id,
                                                  itemMaterialId: m.id,
                                                  pullQty: Math.min(
                                                    Number(m.quantity || 1),
                                                    Number(stockSku.on_hand_qty || 0)
                                                  ),
                                                })
                                              }
                                              className="min-h-[36px] px-2.5 rounded-lg bg-emerald-50 border border-emerald-300 text-emerald-900 text-[11px] font-bold"
                                            >
                                              🟢 {fmtQty(stockSku.on_hand_qty)} in Stock · Pull
                                            </button>
                                          )}

                                          <select
                                            value={m.status || "needed"}
                                            disabled={busyKey === m.id}
                                            onChange={(e) =>
                                              handleMaterialStatusPatch(m.id, e.target.value)
                                            }
                                            className={`min-h-[36px] px-2 rounded-lg border text-xs font-bold ${
                                              MATERIAL_STATUS_PILL_CLASS[m.status || "needed"]
                                            }`}
                                          >
                                            {MATERIAL_STATUS_OPTIONS.map((opt) => (
                                              <option key={opt.code} value={opt.code}>
                                                {opt.label}
                                              </option>
                                            ))}
                                          </select>

                                          <button
                                            type="button"
                                            disabled={busyKey === m.id}
                                            onClick={() => handleDeleteMaterial(m.id)}
                                            title="Remove material line"
                                            className="min-h-[36px] min-w-[36px] rounded-lg border border-stone-200 text-stone-400 hover:text-rose-700 hover:bg-rose-50 flex items-center justify-center"
                                          >
                                            <Trash2 className="w-3.5 h-3.5" />
                                          </button>
                                        </div>
                                      </div>
                                    );
                                  })}
                                </div>
                              )}

                              {/* Fast 1-Line Quick Add Material */}
                              <form
                                onSubmit={(e) => handleAddMaterial(e, it.id)}
                                className="flex flex-wrap sm:flex-nowrap items-center gap-1.5 pt-1"
                              >
                                <input
                                  value={matDraft.material_name}
                                  onChange={(e) =>
                                    setNewMatForm((prev) => ({
                                      ...prev,
                                      [it.id]: {
                                        ...matDraft,
                                        material_name: e.target.value,
                                      },
                                    }))
                                  }
                                  placeholder="Add material (e.g. 18mm HDHMR Ply, Ebco Hinges)"
                                  className="flex-1 min-w-[160px] min-h-[42px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                                />
                                <input
                                  type="number"
                                  min="0.01"
                                  step="any"
                                  value={matDraft.quantity}
                                  onChange={(e) =>
                                    setNewMatForm((prev) => ({
                                      ...prev,
                                      [it.id]: { ...matDraft, quantity: e.target.value },
                                    }))
                                  }
                                  className="w-16 min-h-[42px] px-2 rounded-lg border border-stone-300 text-xs bg-white text-center font-semibold"
                                />
                                <select
                                  value={matDraft.unit}
                                  onChange={(e) =>
                                    setNewMatForm((prev) => ({
                                      ...prev,
                                      [it.id]: { ...matDraft, unit: e.target.value },
                                    }))
                                  }
                                  className="min-h-[42px] px-2 rounded-lg border border-stone-300 text-xs bg-white font-semibold"
                                >
                                  {MATERIAL_UNITS.map((u) => (
                                    <option key={u} value={u}>
                                      {u}
                                    </option>
                                  ))}
                                </select>
                                <button
                                  type="submit"
                                  disabled={busyKey === `mat-${it.id}`}
                                  className="min-h-[42px] px-3 rounded-lg bg-amber-900 text-white text-xs font-bold flex items-center gap-1 shrink-0"
                                >
                                  <Plus className="w-3.5 h-3.5" />
                                  <span>Add</span>
                                </button>
                              </form>
                            </div>

                            {/* Secondary Mistri / Dimensions / Job Card Row */}
                            <div className="pt-2 border-t border-stone-200 flex items-center justify-between gap-2 flex-wrap">
                              <button
                                type="button"
                                onClick={() => {
                                  if (showMistriForm) {
                                    setEditingMistriItemId(null);
                                  } else {
                                    setEditingMistriItemId(it.id);
                                    setItemSpecDraft({
                                      dimensions: it.dimensions || "",
                                      specs_notes: it.specs_notes || "",
                                    });
                                  }
                                }}
                                className="min-h-[38px] px-3 rounded-lg border border-stone-300 bg-white text-xs font-semibold text-stone-700 flex items-center gap-1.5"
                              >
                                <Pencil className="w-3.5 h-3.5" />
                                <span>
                                  {showMistriForm
                                    ? "Hide Mistri & Specs"
                                    : "Edit Mistri & Specs"}
                                </span>
                              </button>

                              <div className="flex items-center gap-2 flex-wrap">
                                {it.pre_pack_photo_path && (
                                  <button
                                    type="button"
                                    disabled={busyKey === `att:${it.pre_pack_photo_path}`}
                                    onClick={() => handleOpenAttachment(it.pre_pack_photo_path)}
                                    className="min-h-[38px] px-3 rounded-lg border border-emerald-200 bg-emerald-50 text-emerald-900 text-xs font-semibold flex items-center gap-1.5"
                                  >
                                    <Image className="w-3.5 h-3.5" />
                                    <span>View Photo</span>
                                  </button>
                                )}

                                <button
                                  type="button"
                                  disabled={busyKey === `item_photo:${it.id}`}
                                  onClick={() => {
                                    photoUploadItemIdRef.current = it.id;
                                    itemPhotoRef.current?.click();
                                  }}
                                  className="min-h-[38px] px-3 rounded-lg border border-stone-300 bg-white text-stone-700 text-xs font-semibold flex items-center gap-1.5"
                                >
                                  <Camera className="w-3.5 h-3.5" />
                                  <span>
                                    {busyKey === `item_photo:${it.id}`
                                      ? "Saving..."
                                      : it.pre_pack_photo_path
                                      ? "Retake Photo"
                                      : "Photo"}
                                  </span>
                                </button>

                                <button
                                  type="button"
                                  onClick={() =>
                                    shareKarigarJobCard({
                                      projectCode: order.project_code,
                                      customerArea: order.site_area_landmark,
                                      item: it,
                                      shopMistriName: shopName,
                                      siteMistriName: siteName,
                                      materials: itemMats,
                                    })
                                  }
                                  className="min-h-[38px] px-3 rounded-lg border border-amber-300 bg-amber-50 text-amber-950 text-xs font-semibold flex items-center gap-1.5"
                                >
                                  <Share2 className="w-3.5 h-3.5" />
                                  <span>Job Card</span>
                                </button>
                              </div>
                            </div>

                            {showMistriForm && (
                              <div className="bg-white rounded-lg border border-stone-200 p-2.5 space-y-2">
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
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

                                <div className="flex flex-col sm:flex-row gap-2">
                                  <input
                                    value={itemSpecDraft.dimensions || ""}
                                    onChange={(e) =>
                                      setItemSpecDraft((prev) => ({
                                        ...prev,
                                        dimensions: e.target.value,
                                      }))
                                    }
                                    placeholder="Dimensions (e.g. 8ft x 6ft)"
                                    className="sm:w-1/3 min-h-[38px] px-2.5 rounded-lg border border-stone-200 text-xs"
                                  />
                                  <input
                                    value={itemSpecDraft.specs_notes || ""}
                                    onChange={(e) =>
                                      setItemSpecDraft((prev) => ({
                                        ...prev,
                                        specs_notes: e.target.value,
                                      }))
                                    }
                                    placeholder="Specs / Finish notes"
                                    className="flex-1 min-h-[38px] px-2.5 rounded-lg border border-stone-200 text-xs"
                                  />
                                  <button
                                    type="button"
                                    onClick={() =>
                                      handleItemPatch(it.id, {
                                        to_stage: it.stage,
                                        dimensions: itemSpecDraft.dimensions || null,
                                        specs_notes: itemSpecDraft.specs_notes || null,
                                      }).then(() => setEditingMistriItemId(null))
                                    }
                                    className="min-h-[38px] px-3 rounded-lg bg-stone-900 text-white text-xs font-bold"
                                  >
                                    Save
                                  </button>
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Quick Add Furniture Item Bar */}
              <form
                onSubmit={handleAddItem}
                className="bg-white rounded-xl border border-dashed border-stone-300 p-3 flex flex-col sm:flex-row gap-2"
              >
                <input
                  value={newItemName}
                  onChange={(e) => setNewItemName(e.target.value)}
                  placeholder="Add furniture item (e.g. Modular Kitchen, Master Bed)"
                  className="flex-1 min-h-[42px] px-3 rounded-lg border border-stone-300 text-xs bg-white"
                />
                <select
                  value={newItemMistri}
                  onChange={(e) => setNewItemMistri(e.target.value)}
                  className="min-h-[42px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                >
                  <option value="">Shop Mistri (optional)</option>
                  {allAssignees.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.full_name}
                    </option>
                  ))}
                </select>
                <button
                  type="submit"
                  disabled={busyKey === "add-item"}
                  className="min-h-[42px] px-4 rounded-lg bg-stone-900 text-white text-xs font-bold shrink-0"
                >
                  + Add Item
                </button>
              </form>
            </>
          ) : (
            /* Customer-Level Material Rollup View */
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-1.5">
                  {[
                    { id: "all", label: `All (${allMaterials.length})` },
                    { id: "needed", label: "To Order" },
                    { id: "ordered", label: "Ordered" },
                    { id: "in_shop", label: "In Shop" },
                  ].map((f) => (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() => setMatFilter(f.id)}
                      className={`min-h-[36px] px-3 rounded-full text-xs font-semibold transition ${
                        matFilter === f.id
                          ? "bg-amber-900 text-white"
                          : "bg-white border border-stone-200 text-stone-700"
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>

                {allMaterials.length > 0 && (
                  <button
                    type="button"
                    onClick={handleCopyToOrderWhatsApp}
                    className="min-h-[38px] px-3 rounded-xl border border-amber-300 bg-amber-50 text-amber-950 text-xs font-bold flex items-center gap-1.5"
                  >
                    {copiedMsg ? (
                      <>
                        <Check className="w-3.5 h-3.5 text-emerald-700" />
                        <span>Copied!</span>
                      </>
                    ) : (
                      <>
                        <Copy className="w-3.5 h-3.5" />
                        <span>Copy &ldquo;To Order&rdquo; List</span>
                      </>
                    )}
                  </button>
                )}
              </div>

              {groupedByCategory.length === 0 ? (
                <div className="bg-white rounded-xl border border-stone-200 p-6 text-center text-xs text-stone-500">
                  {allMaterials.length === 0
                    ? 'No raw materials added yet. Open any Furniture Item (or tell the AI bar e.g. "Kitchen mein 12 sheet 18mm ply add karo") and they will automatically roll up here.'
                    : "No materials match this status filter."}
                </div>
              ) : (
                groupedByCategory.map(({ category, rows }) => (
                  <div
                    key={category.code}
                    className="bg-white rounded-xl border border-stone-200 overflow-hidden"
                  >
                    <div className="bg-stone-100/80 px-3.5 py-2 border-b border-stone-200 flex items-center justify-between">
                      <span className="text-xs font-extrabold uppercase tracking-wide text-stone-700">
                        {category.label}
                      </span>
                      <span className="text-[11px] font-semibold text-stone-500">
                        {rows.length} item{rows.length === 1 ? "" : "s"}
                      </span>
                    </div>
                    <div className="divide-y divide-stone-100">
                      {rows.map((g) => {
                        const statusVal =
                          g.statuses.size === 1
                            ? Array.from(g.statuses)[0]
                            : "mixed";
                        const rollupStockSku =
                          g.firstNeededRow
                            ? matchStockSkuForMaterial(inventoryItems, g.firstNeededRow)
                            : null;
                        return (
                          <div
                            key={g.key}
                            className="px-3.5 py-2.5 flex flex-wrap sm:flex-nowrap items-center justify-between gap-2"
                          >
                            <div className="min-w-0 flex-1 space-y-0.5">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="font-bold text-sm text-stone-900">
                                  {g.materialName}
                                </span>
                                <span className="text-xs font-extrabold text-amber-950 bg-amber-100 px-2 py-0.5 rounded-md">
                                  {fmtQty(g.totalQty)} {g.unit}
                                </span>
                              </div>
                              <div className="text-xs text-stone-500">
                                {g.breakdown
                                  .map((b) => `${b.itemName}: ${fmtQty(b.qty)}`)
                                  .join(" · ")}
                              </div>
                            </div>

                            <div className="flex items-center gap-1.5 flex-wrap shrink-0">
                              {rollupStockSku && g.firstNeededRow && (
                                <button
                                  type="button"
                                  disabled={busyKey === `pull-${g.firstNeededRow.id}`}
                                  onClick={() =>
                                    handlePullStockForMaterial(rollupStockSku, {
                                      projectId: order.id,
                                      itemId: g.firstNeededRow.item_id || null,
                                      itemMaterialId: g.firstNeededRow.id,
                                      pullQty: Math.min(
                                        Number(g.firstNeededRow.quantity || 1),
                                        Number(rollupStockSku.on_hand_qty || 0)
                                      ),
                                    })
                                  }
                                  className="min-h-[38px] px-2.5 rounded-lg bg-emerald-50 border border-emerald-300 text-emerald-900 text-[11px] font-bold"
                                >
                                  🟢 {fmtQty(rollupStockSku.on_hand_qty)} in Stock · Pull
                                </button>
                              )}

                              <select
                                value={statusVal}
                                disabled={busyKey === `bulk-${g.materialIds[0]}`}
                                onChange={(e) => {
                                  if (e.target.value !== "mixed") {
                                    handleBulkStatus(g.materialIds, e.target.value);
                                  }
                                }}
                                className={`min-h-[38px] px-2.5 rounded-lg border text-xs font-bold shrink-0 ${
                                  MATERIAL_STATUS_PILL_CLASS[statusVal] ||
                                  MATERIAL_STATUS_PILL_CLASS.mixed
                                }`}
                              >
                                {statusVal === "mixed" && (
                                  <option value="mixed" disabled>
                                    Mixed
                                  </option>
                                )}
                                {MATERIAL_STATUS_OPTIONS.map((opt) => (
                                  <option key={opt.code} value={opt.code}>
                                    {opt.label}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))
              )}

              {/* Quick Add Order-Wide Material Bar */}
              <form
                onSubmit={handleAddOrderWideMaterial}
                className="bg-white rounded-xl border border-dashed border-stone-300 p-3 flex flex-wrap sm:flex-nowrap items-center gap-1.5"
              >
                <input
                  value={orderWideMat.material_name}
                  onChange={(e) =>
                    setOrderWideMat((prev) => ({ ...prev, material_name: e.target.value }))
                  }
                  placeholder="+ Add Order-Wide material (e.g. Fevicol SH 5kg, Satin Charcoal Louver)"
                  className="flex-1 min-w-[180px] min-h-[42px] px-2.5 rounded-lg border border-stone-300 text-xs bg-white"
                />
                <input
                  type="number"
                  min="0.01"
                  step="any"
                  value={orderWideMat.quantity}
                  onChange={(e) =>
                    setOrderWideMat((prev) => ({ ...prev, quantity: e.target.value }))
                  }
                  className="w-16 min-h-[42px] px-2 rounded-lg border border-stone-300 text-xs bg-white text-center font-semibold"
                />
                <select
                  value={orderWideMat.unit}
                  onChange={(e) =>
                    setOrderWideMat((prev) => ({ ...prev, unit: e.target.value }))
                  }
                  className="min-h-[42px] px-2 rounded-lg border border-stone-300 text-xs bg-white font-semibold"
                >
                  {MATERIAL_UNITS.map((u) => (
                    <option key={u} value={u}>
                      {u}
                    </option>
                  ))}
                </select>
                <button
                  type="submit"
                  disabled={busyKey === "mat-order-wide"}
                  className="min-h-[42px] px-3 rounded-lg bg-amber-900 text-white text-xs font-bold flex items-center gap-1 shrink-0"
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>Add Order-Wide</span>
                </button>
              </form>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
