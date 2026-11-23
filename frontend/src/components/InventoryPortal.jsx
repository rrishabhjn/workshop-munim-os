import { useMemo, useState } from "react";
import {
  ArrowDownUp,
  ArrowRightLeft,
  CheckCircle2,
  ClipboardCheck,
  IndianRupee,
  Package,
  Pencil,
  Plus,
  Search,
  ShoppingCart,
  SlidersHorizontal,
  Undo2,
  X,
} from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import { suggestOpenOrderMaterial } from "../lib/inventoryMatch";

const UNIT_OPTIONS = [
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

const MOVEMENT_TYPE_LABELS = {
  stock_load: "Stock Load",
  direct_order_receipt: "Direct Order Receipt",
  customer_attribution: "Pull → Order",
  count_adjust: "± Count Adjust",
  undo: "Reversal",
};

function fmtQty(val) {
  const n = Number(val || 0);
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)));
}

function parseApiError(err, fallback = "Operation failed.") {
  const raw = String(err?.message || "").trim();
  if (!raw) return fallback;
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw);
      return (
        parsed?.detail ||
        parsed?.error?.message ||
        (typeof parsed?.error === "string" ? parsed.error : null) ||
        parsed?.message ||
        fallback
      );
    } catch {
      return raw;
    }
  }
  return raw;
}

function emptyLoadLine(defaultDestination = "stock") {
  return {
    key: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    destination: defaultDestination,
    inventory_item_id: "",
    is_new_sku: false,
    material_name: "",
    category: "ply_wood",
    unit: "pcs",
    min_stock_qty: "0",
    quantity: "1",
    unit_rate_inr: "",
    project_id: "",
    item_id: "",
    matched_item_material_id: "",
    fulfill_manual: false,
  };
}

export default function InventoryPortal({ inventoryData, onRefresh, onOpenOrder }) {
  const [subTab, setSubTab] = useState("stock");

  // Stock table controls (flat sortable table, Category as sortable column)
  const [stockSearch, setStockSearch] = useState("");
  const [lowStockOnly, setLowStockOnly] = useState(false);
  const [stockCatFilter, setStockCatFilter] = useState("all");
  const [stockSort, setStockSort] = useState({ key: "name", dir: "asc" });

  // To Buy & Gate-In table controls
  const [buySearch, setBuySearch] = useState("");
  const [buyStatusFilter, setBuyStatusFilter] = useState("all");
  const [buyCatFilter, setBuyCatFilter] = useState("all");
  const [buySort, setBuySort] = useState({ key: "material_name", dir: "asc" });

  // Stock Ledger controls
  const [ledgerSearch, setLedgerSearch] = useState("");
  const [ratePendingOnly, setRatePendingOnly] = useState(false);

  // Action feedback & busy state
  const [busyKey, setBusyKey] = useState("");
  const [bannerMsg, setBannerMsg] = useState("");
  const [modalErr, setModalErr] = useState("");

  // Modals
  const [loadModal, setLoadModal] = useState(null);
  const [attrModal, setAttrModal] = useState(null);
  const [countModal, setCountModal] = useState(null);
  const [skuModal, setSkuModal] = useState(null);
  const [rateModal, setRateModal] = useState(null);

  const items = inventoryData?.items || [];
  const toBuy = inventoryData?.to_buy || [];
  const movements = inventoryData?.movements || [];
  const kpis = inventoryData?.kpis || {
    total_stock_value_inr: 0,
    sku_count: 0,
    low_stock_count: 0,
    rate_pending_count: 0,
  };
  const categories = inventoryData?.categories || [
    { code: "ply_wood", label: "Ply / Wood" },
    { code: "laminate", label: "Laminate / Sunmica" },
    { code: "hardware", label: "Hardware" },
    { code: "foam_fabric", label: "Foam / Fabric" },
    { code: "polish", label: "Polish / Adhesive" },
  ];
  const activeOrders = inventoryData?.active_orders || [];
  const vendors = inventoryData?.vendors || [];
  const accounts = inventoryData?.payment_accounts || [];

  const openOrderMaterialsByProject = useMemo(() => {
    const map = {};
    for (const row of toBuy) {
      for (const ol of row.order_lines || []) {
        if (!map[ol.project_id]) map[ol.project_id] = [];
        map[ol.project_id].push({
          ...ol,
          material_name: row.material_name,
          unit: row.unit,
          category: row.category,
        });
      }
    }
    return map;
  }, [toBuy]);

  const filteredStock = useMemo(() => {
    const q = stockSearch.trim().toLowerCase();
    const list = items.filter((it) => {
      if (lowStockOnly && !it.is_low_stock) return false;
      if (stockCatFilter !== "all" && it.category !== stockCatFilter) return false;
      if (!q) return true;
      const hay = [it.name, it.category_label, ...(it.aliases || [])]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
    const dir = stockSort.dir === "desc" ? -1 : 1;
    return [...list].sort((a, b) => {
      const k = stockSort.key;
      if (k === "on_hand_qty" || k === "min_stock_qty" || k === "avg_unit_cost_inr" || k === "stock_value_inr") {
        return (Number(a[k] || 0) - Number(b[k] || 0)) * dir;
      }
      const va = String(a[k === "category" ? "category_label" : k] || "").toLowerCase();
      const vb = String(b[k === "category" ? "category_label" : k] || "").toLowerCase();
      return va.localeCompare(vb) * dir;
    });
  }, [items, stockSearch, lowStockOnly, stockCatFilter, stockSort]);

  const filteredToBuy = useMemo(() => {
    const q = buySearch.trim().toLowerCase();
    const list = toBuy.filter((r) => {
      if (buyStatusFilter !== "all" && r.status !== buyStatusFilter) return false;
      if (buyCatFilter !== "all" && r.category !== buyCatFilter) return false;
      if (!q) return true;
      const ordText = (r.order_lines || [])
        .map((ol) => `${ol.project_code} ${ol.customer_name} ${ol.item_name}`)
        .join(" ");
      return `${r.material_name} ${r.category_label} ${ordText}`.toLowerCase().includes(q);
    });
    const dir = buySort.dir === "desc" ? -1 : 1;
    return [...list].sort((a, b) => {
      const k = buySort.key;
      if (k === "total_demand_qty" || k === "in_stock_qty") {
        return (Number(a[k] || 0) - Number(b[k] || 0)) * dir;
      }
      const va = String(a[k === "category" ? "category_label" : k] || "").toLowerCase();
      const vb = String(b[k === "category" ? "category_label" : k] || "").toLowerCase();
      return va.localeCompare(vb) * dir;
    });
  }, [toBuy, buySearch, buyStatusFilter, buyCatFilter, buySort]);

  const filteredMovements = useMemo(() => {
    const q = ledgerSearch.trim().toLowerCase();
    return movements.filter((mv) => {
      if (ratePendingOnly && !(mv.zoho_sync_status === "rate_pending" && !mv.is_undone)) {
        return false;
      }
      if (!q) return true;
      const hay = [
        mv.material_label,
        mv.category_label,
        mv.customer_name,
        mv.project_code,
        mv.vendor_name,
        mv.account_name,
        mv.invoice_no,
        mv.notes,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [movements, ledgerSearch, ratePendingOnly]);

  if (!inventoryData) {
    return (
      <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center text-sm text-stone-500">
        Loading Workshop Inventory...
      </div>
    );
  }

  function toggleSort(setter, key) {
    setter((prev) => ({
      key,
      dir: prev.key === key && prev.dir === "asc" ? "desc" : "asc",
    }));
  }

  function openLoadStockModal(prefillLine = null) {
    setModalErr("");
    const line = prefillLine ? { ...emptyLoadLine(prefillLine.destination || "stock"), ...prefillLine } : emptyLoadLine("stock");
    if (!line.project_id && line.destination === "order" && activeOrders[0]) {
      line.project_id = activeOrders[0].id;
    }
    setLoadModal({
      settlement_mode: "paid_now",
      payment_account_id: accounts[0]?.id || "",
      vendor_id: "",
      vendor_name: "",
      invoice_no: "",
      notes: "",
      lines: [line],
    });
  }

  function openAttributeModal(prefill = {}) {
    setModalErr("");
    const firstSku =
      items.find((i) => i.id === prefill.inventory_item_id) ||
      items.find((i) => Number(i.on_hand_qty || 0) > 0) ||
      items[0];
    setAttrModal({
      inventory_item_id: prefill.inventory_item_id || firstSku?.id || "",
      project_id: prefill.project_id || activeOrders[0]?.id || "",
      item_id: prefill.item_id || "",
      item_material_id: prefill.item_material_id || "",
      quantity: prefill.quantity ? String(prefill.quantity) : "1",
      notes: "",
    });
  }

  function updateLoadLine(idx, patch) {
    setLoadModal((prev) => {
      if (!prev) return prev;
      const nextLines = prev.lines.map((ln, i) => {
        if (i !== idx) return ln;
        const merged = { ...ln, ...patch };
        if (merged.destination === "order" && !merged.fulfill_manual) {
          const openList = openOrderMaterialsByProject[merged.project_id] || [];
          const hit = suggestOpenOrderMaterial(openList, merged.material_name);
          merged.matched_item_material_id = hit ? hit.material_id : "";
          if (hit?.item_id) merged.item_id = hit.item_id;
        }
        return merged;
      });
      return { ...prev, lines: nextLines };
    });
  }

  async function handleSubmitLoad(e) {
    e.preventDefault();
    if (!loadModal) return;
    setModalErr("");
    setBusyKey("submit-load");
    try {
      const payloadLines = loadModal.lines.map((ln) => {
        if (ln.destination === "stock") {
          const creatingNew = ln.is_new_sku || !ln.inventory_item_id;
          return {
            destination: "stock",
            inventory_item_id: creatingNew ? null : ln.inventory_item_id,
            material_name: creatingNew
              ? ln.material_name.trim()
              : items.find((it) => it.id === ln.inventory_item_id)?.name || ln.material_name.trim(),
            category: ln.category,
            unit: ln.unit,
            min_stock_qty: Number(ln.min_stock_qty || 0),
            quantity: Number(ln.quantity || 0),
            unit_rate_inr:
              loadModal.settlement_mode === "rate_pending"
                ? 0
                : ln.unit_rate_inr === ""
                ? null
                : Number(ln.unit_rate_inr),
          };
        }
        return {
          destination: "order",
          project_id: ln.project_id || null,
          item_id: ln.item_id || null,
          matched_item_material_id: ln.matched_item_material_id || null,
          material_name: ln.material_name.trim(),
          category: ln.category,
          unit: ln.unit,
          quantity: Number(ln.quantity || 0),
          unit_rate_inr:
            loadModal.settlement_mode === "rate_pending"
              ? 0
              : ln.unit_rate_inr === ""
              ? null
              : Number(ln.unit_rate_inr),
        };
      });

      const res = await apiFetch("/api/inventory/load", {
        method: "POST",
        body: JSON.stringify({
          settlement_mode: loadModal.settlement_mode,
          payment_account_id:
            loadModal.settlement_mode === "paid_now" ? loadModal.payment_account_id || null : null,
          vendor_id: loadModal.vendor_id || null,
          vendor_name: loadModal.vendor_id ? null : loadModal.vendor_name.trim() || null,
          invoice_no: loadModal.invoice_no.trim() || null,
          notes: loadModal.notes.trim() || null,
          lines: payloadLines,
        }),
      });
      setLoadModal(null);
      setBannerMsg(res?.receipt || "Stock loaded.");
      await onRefresh();
    } catch (err) {
      setModalErr(parseApiError(err, "Failed loading inventory."));
    } finally {
      setBusyKey("");
    }
  }

  async function handleSubmitAttribute(e) {
    e.preventDefault();
    if (!attrModal) return;
    setModalErr("");
    setBusyKey("submit-attr");
    try {
      const res = await apiFetch("/api/inventory/attribute", {
        method: "POST",
        body: JSON.stringify({
          inventory_item_id: attrModal.inventory_item_id,
          project_id: attrModal.project_id,
          item_id: attrModal.item_id || null,
          item_material_id: attrModal.item_material_id || null,
          quantity: Number(attrModal.quantity || 0),
          notes: attrModal.notes.trim() || null,
        }),
      });
      setAttrModal(null);
      setBannerMsg(res?.receipt || "Stock pulled to order.");
      await onRefresh();
    } catch (err) {
      setModalErr(parseApiError(err, "Failed attributing stock."));
    } finally {
      setBusyKey("");
    }
  }

  async function handleSubmitCountAdjust(e) {
    e.preventDefault();
    if (!countModal) return;
    setModalErr("");
    setBusyKey("submit-count");
    try {
      const res = await apiFetch("/api/inventory/count-adjust", {
        method: "POST",
        body: JSON.stringify({
          inventory_item_id: countModal.sku.id,
          actual_qty: Number(countModal.actual_qty || 0),
          notes: countModal.notes.trim() || null,
        }),
      });
      setCountModal(null);
      setBannerMsg(res?.receipt || "Count adjusted.");
      await onRefresh();
    } catch (err) {
      setModalErr(parseApiError(err, "Failed adjusting count."));
    } finally {
      setBusyKey("");
    }
  }

  async function handleSubmitSku(e) {
    e.preventDefault();
    if (!skuModal) return;
    setModalErr("");
    setBusyKey("submit-sku");
    try {
      const payload = {
        name: skuModal.name.trim(),
        category: skuModal.category,
        unit: skuModal.unit,
        min_stock_qty: Number(skuModal.min_stock_qty || 0),
        avg_unit_cost_inr:
          skuModal.avg_unit_cost_inr !== "" ? Number(skuModal.avg_unit_cost_inr) : null,
        aliases: skuModal.aliasesText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        notes: skuModal.notes.trim() || null,
      };
      const path = skuModal.sku_id
        ? `/api/inventory/items/${skuModal.sku_id}`
        : "/api/inventory/items";
      const method = skuModal.sku_id ? "PATCH" : "POST";
      const res = await apiFetch(path, { method, body: JSON.stringify(payload) });
      setSkuModal(null);
      setBannerMsg(res?.receipt || "SKU saved.");
      await onRefresh();
    } catch (err) {
      setModalErr(parseApiError(err, "Failed saving SKU."));
    } finally {
      setBusyKey("");
    }
  }

  async function handleSubmitRate(e) {
    e.preventDefault();
    if (!rateModal) return;
    setModalErr("");
    setBusyKey("submit-rate");
    try {
      const res = await apiFetch(
        `/api/inventory/movements/${rateModal.mv.id}/enter-rate`,
        {
          method: "POST",
          body: JSON.stringify({
            settlement_mode: rateModal.settlement_mode,
            unit_rate_inr: Number(rateModal.unit_rate_inr || 0),
            payment_account_id:
              rateModal.settlement_mode === "paid_now"
                ? rateModal.payment_account_id || null
                : null,
            vendor_id: rateModal.vendor_id || null,
            vendor_name: rateModal.vendor_id ? null : rateModal.vendor_name.trim() || null,
            invoice_no: rateModal.invoice_no.trim() || null,
            notes: rateModal.notes.trim() || null,
          }),
        }
      );
      setRateModal(null);
      setBannerMsg(res?.receipt || "Rate entered and synced.");
      await onRefresh();
    } catch (err) {
      setModalErr(parseApiError(err, "Failed entering rate."));
    } finally {
      setBusyKey("");
    }
  }

  async function handleMarkRowOrdered(row) {
    const neededByProject = {};
    for (const ol of row.order_lines || []) {
      if (ol.status === "needed") {
        if (!neededByProject[ol.project_id]) neededByProject[ol.project_id] = [];
        neededByProject[ol.project_id].push(ol.material_id);
      }
    }
    const entries = Object.entries(neededByProject);
    if (!entries.length) return;
    setBusyKey(`ord-${row.key}`);
    try {
      for (const [pid, materialIds] of entries) {
        await apiFetch(`/api/production/orders/${pid}/materials/bulk-status`, {
          method: "POST",
          body: JSON.stringify({ material_ids: materialIds, status: "ordered" }),
        });
      }
      setBannerMsg(`Marked "${row.material_name}" as Ordered.`);
      await onRefresh();
    } catch (err) {
      setBannerMsg(parseApiError(err, "Failed marking ordered."));
    } finally {
      setBusyKey("");
    }
  }

  async function handleUndoMovement(mv) {
    setBusyKey(`undo-${mv.id}`);
    try {
      const res = await apiFetch(`/api/inventory/movements/${mv.id}/undo`, {
        method: "POST",
      });
      setBannerMsg(res?.receipt || "Movement undone.");
      await onRefresh();
    } catch (err) {
      setBannerMsg(parseApiError(err, "Could not undo movement."));
    } finally {
      setBusyKey("");
    }
  }

  return (
    <div className="space-y-4 pb-28">
      {/* Feedback Banner */}
      {bannerMsg && (
        <div className="bg-emerald-50 border border-emerald-300 text-emerald-950 rounded-xl px-3.5 py-2.5 text-xs font-semibold flex items-center justify-between gap-2">
          <span>{bannerMsg.replace(/[*`]/g, "")}</span>
          <button
            type="button"
            onClick={() => setBannerMsg("")}
            className="min-h-[36px] min-w-[36px] flex items-center justify-center rounded-lg hover:bg-emerald-100"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* KPI Strip + Primary Actions */}
      <div className="grid grid-cols-3 gap-2.5">
        <div className="bg-white rounded-2xl border border-stone-200 p-3 shadow-xs">
          <div className="text-[11px] font-bold uppercase tracking-wide text-stone-500">
            Stock Value
          </div>
          <div className="text-lg sm:text-xl font-extrabold text-amber-950 mt-0.5">
            {formatINR(kpis.total_stock_value_inr)}
          </div>
          <div className="text-[11px] text-stone-500">{kpis.sku_count} active SKUs</div>
        </div>

        <button
          type="button"
          onClick={() => {
            setSubTab("stock");
            setLowStockOnly((v) => !v);
          }}
          className={`text-left rounded-2xl border p-3 shadow-xs transition ${
            lowStockOnly && subTab === "stock"
              ? "bg-amber-50 border-amber-400"
              : "bg-white border-stone-200 hover:bg-stone-50"
          }`}
        >
          <div className="text-[11px] font-bold uppercase tracking-wide text-stone-500">
            Low Stock Alerts
          </div>
          <div
            className={`text-lg sm:text-xl font-extrabold mt-0.5 ${
              kpis.low_stock_count > 0 ? "text-amber-700" : "text-stone-900"
            }`}
          >
            {kpis.low_stock_count}
          </div>
          <div className="text-[11px] text-stone-500">Tap to filter</div>
        </button>

        <button
          type="button"
          onClick={() => {
            setSubTab("ledger");
            setRatePendingOnly((v) => !v);
          }}
          className={`text-left rounded-2xl border p-3 shadow-xs transition ${
            ratePendingOnly && subTab === "ledger"
              ? "bg-amber-50 border-amber-400"
              : "bg-white border-stone-200 hover:bg-stone-50"
          }`}
        >
          <div className="text-[11px] font-bold uppercase tracking-wide text-stone-500">
            Rate Pending
          </div>
          <div
            className={`text-lg sm:text-xl font-extrabold mt-0.5 ${
              kpis.rate_pending_count > 0 ? "text-rose-700" : "text-stone-900"
            }`}
          >
            {kpis.rate_pending_count}
          </div>
          <div className="text-[11px] text-stone-500">Tap to enter rates</div>
        </button>
      </div>

      {/* Primary Action Strip */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => openLoadStockModal(null)}
          className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold flex items-center gap-1.5 shadow-sm"
        >
          <Plus className="w-4 h-4" />
          <span>Load Stock / Gate-In</span>
        </button>
        <button
          type="button"
          disabled={items.every((i) => Number(i.on_hand_qty || 0) <= 0)}
          onClick={() => openAttributeModal({})}
          className="min-h-[44px] px-4 rounded-xl border border-stone-300 bg-white text-stone-800 text-xs font-bold flex items-center gap-1.5 disabled:opacity-45"
        >
          <ArrowRightLeft className="w-4 h-4 text-emerald-700" />
          <span>Attribute to Order</span>
        </button>
        <button
          type="button"
          onClick={() => {
            setModalErr("");
            setSkuModal({
              sku_id: null,
              name: "",
              category: "ply_wood",
              unit: "sheets",
              min_stock_qty: "0",
              avg_unit_cost_inr: "",
              aliasesText: "",
              notes: "",
            });
          }}
          className="min-h-[44px] px-3.5 rounded-xl border border-stone-300 bg-white text-stone-700 text-xs font-bold flex items-center gap-1.5"
        >
          <Plus className="w-4 h-4" />
          <span>Add SKU</span>
        </button>
      </div>

      {/* 3 Sub-Tabs */}
      <div className="grid grid-cols-3 gap-1.5 bg-white p-1.5 rounded-2xl border border-stone-200">
        {[
          { id: "stock", label: `📦 Workshop Stock (${items.length})` },
          { id: "to_buy", label: `🛒 To Buy & Gate-In (${toBuy.length})` },
          { id: "ledger", label: `📜 Stock Ledger (${movements.length})` },
        ].map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setSubTab(t.id)}
            className={`min-h-[44px] px-2 rounded-xl text-xs font-bold transition ${
              subTab === t.id
                ? "bg-stone-900 text-white shadow-xs"
                : "text-stone-600 hover:bg-stone-100"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* SUB-TAB 1: WORKSHOP STOCK */}
      {subTab === "stock" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="w-4 h-4 text-stone-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                value={stockSearch}
                onChange={(e) => setStockSearch(e.target.value)}
                placeholder="Search SKU or alias (e.g. 18mm HDHMR, mica, kabje)..."
                className="w-full min-h-[44px] pl-9 pr-3 rounded-xl border border-stone-200 bg-white text-xs"
              />
            </div>
            <select
              value={stockCatFilter}
              onChange={(e) => setStockCatFilter(e.target.value)}
              className="min-h-[44px] px-3 rounded-xl border border-stone-200 bg-white text-xs font-semibold"
            >
              <option value="all">All Categories</option>
              {categories.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setLowStockOnly((v) => !v)}
              className={`min-h-[44px] px-3.5 rounded-xl border text-xs font-bold ${
                lowStockOnly
                  ? "bg-amber-900 text-white border-amber-900"
                  : "bg-white text-stone-700 border-stone-200"
              }`}
            >
              Low Stock Only
            </button>
          </div>

          {filteredStock.length === 0 ? (
            <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center space-y-3">
              <Package className="w-8 h-8 text-stone-400 mx-auto" />
              <div className="text-sm font-bold text-stone-800">
                {items.length === 0
                  ? "Workshop Stock catalog is clean & ready"
                  : "No SKUs match these filters"}
              </div>
              <p className="text-xs text-stone-500 max-w-md mx-auto">
                {items.length === 0
                  ? "Start empty and build your real Beawar SKU catalog as deliveries arrive at Gate-In or via [+ Add SKU]."
                  : "Try clearing search, category, or Low Stock filter."}
              </p>
              {items.length === 0 && (
                <div className="flex justify-center gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => openLoadStockModal(null)}
                    className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white text-xs font-bold"
                  >
                    + Load First Delivery
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="bg-white rounded-2xl border border-stone-200 overflow-x-auto shadow-xs">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-stone-200 bg-stone-50 text-stone-600">
                    {[
                      { key: "name", label: "SKU / Item" },
                      { key: "category", label: "Category" },
                      { key: "on_hand_qty", label: "On Hand" },
                      { key: "min_stock_qty", label: "Min Alert" },
                      { key: "avg_unit_cost_inr", label: "Avg Cost" },
                      { key: "stock_value_inr", label: "Stock Value" },
                    ].map((col) => (
                      <th key={col.key} className="px-3 py-2.5 font-bold whitespace-nowrap">
                        <button
                          type="button"
                          onClick={() => toggleSort(setStockSort, col.key)}
                          className="inline-flex items-center gap-1 hover:text-stone-950"
                        >
                          <span>{col.label}</span>
                          <ArrowDownUp className="w-3 h-3 opacity-60" />
                        </button>
                      </th>
                    ))}
                    <th className="px-3 py-2.5 font-bold text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-100">
                  {filteredStock.map((it) => (
                    <tr key={it.id} className="hover:bg-stone-50/70">
                      <td className="px-3 py-2.5">
                        <div className="font-bold text-stone-900">{it.name}</div>
                        {(it.aliases || []).length > 0 && (
                          <div className="text-[11px] text-stone-400">
                            Aliases: {it.aliases.join(", ")}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap">
                        <span className="px-2 py-0.5 rounded-full bg-stone-100 text-stone-700 font-semibold">
                          {it.category_label}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap">
                        <span
                          className={`font-extrabold ${
                            it.is_low_stock ? "text-amber-700" : "text-stone-900"
                          }`}
                        >
                          {fmtQty(it.on_hand_qty)} {it.unit}
                        </span>
                        {it.is_low_stock && (
                          <span className="ml-1.5 text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-100 text-amber-900">
                            Low
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap text-stone-600">
                        {it.min_stock_qty > 0 ? `${fmtQty(it.min_stock_qty)} ${it.unit}` : "—"}
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap font-semibold text-stone-700">
                        {formatINR(it.avg_unit_cost_inr)}
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap font-bold text-stone-900">
                        {formatINR(it.stock_value_inr)}
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap text-right">
                        <div className="inline-flex items-center gap-1.5">
                          <button
                            type="button"
                            disabled={Number(it.on_hand_qty || 0) <= 0}
                            onClick={() => openAttributeModal({ inventory_item_id: it.id })}
                            className="min-h-[40px] px-2.5 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-900 font-bold disabled:opacity-40"
                          >
                            → Attribute
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setModalErr("");
                              setCountModal({
                                sku: it,
                                actual_qty: String(it.on_hand_qty),
                                notes: "",
                              });
                            }}
                            className="min-h-[40px] px-2.5 rounded-lg border border-stone-200 bg-white text-stone-700 font-semibold"
                          >
                            ± Count
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setModalErr("");
                              setSkuModal({
                                sku_id: it.id,
                                name: it.name,
                                category: it.category,
                                unit: it.unit,
                                min_stock_qty: String(it.min_stock_qty || 0),
                                avg_unit_cost_inr: "",
                                aliasesText: (it.aliases || []).join(", "),
                                notes: it.notes || "",
                              });
                            }}
                            className="min-h-[40px] px-2 rounded-lg border border-stone-200 bg-white text-stone-600"
                            title="Edit SKU"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* SUB-TAB 2: TO BUY & GATE-IN (CUJ 1) */}
      {subTab === "to_buy" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="w-4 h-4 text-stone-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                value={buySearch}
                onChange={(e) => setBuySearch(e.target.value)}
                placeholder="Search material, customer or #PRJ code..."
                className="w-full min-h-[44px] pl-9 pr-3 rounded-xl border border-stone-200 bg-white text-xs"
              />
            </div>
            <select
              value={buyStatusFilter}
              onChange={(e) => setBuyStatusFilter(e.target.value)}
              className="min-h-[44px] px-3 rounded-xl border border-stone-200 bg-white text-xs font-semibold"
            >
              <option value="all">All Statuses</option>
              <option value="needed">To Order</option>
              <option value="ordered">Ordered</option>
              <option value="low_stock">Low Stock Restock</option>
            </select>
            <select
              value={buyCatFilter}
              onChange={(e) => setBuyCatFilter(e.target.value)}
              className="min-h-[44px] px-3 rounded-xl border border-stone-200 bg-white text-xs font-semibold"
            >
              <option value="all">All Categories</option>
              {categories.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>

          {filteredToBuy.length === 0 ? (
            <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center space-y-2">
              <ShoppingCart className="w-8 h-8 text-stone-400 mx-auto" />
              <div className="text-sm font-bold text-stone-800">Purchase Queue Clear</div>
              <p className="text-xs text-stone-500">
                All active order materials are In Shop and no standing SKUs are below minimum alert.
              </p>
            </div>
          ) : (
            <div className="bg-white rounded-2xl border border-stone-200 overflow-x-auto shadow-xs">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-stone-200 bg-stone-50 text-stone-600">
                    {[
                      { key: "material_name", label: "Material" },
                      { key: "category", label: "Category" },
                      { key: "total_demand_qty", label: "Demand" },
                      { key: "in_stock_qty", label: "In Stock" },
                    ].map((col) => (
                      <th key={col.key} className="px-3 py-2.5 font-bold whitespace-nowrap">
                        <button
                          type="button"
                          onClick={() => toggleSort(setBuySort, col.key)}
                          className="inline-flex items-center gap-1 hover:text-stone-950"
                        >
                          <span>{col.label}</span>
                          <ArrowDownUp className="w-3 h-3 opacity-60" />
                        </button>
                      </th>
                    ))}
                    <th className="px-3 py-2.5 font-bold">Orders</th>
                    <th className="px-3 py-2.5 font-bold">Status</th>
                    <th className="px-3 py-2.5 font-bold text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-100">
                  {filteredToBuy.map((row) => {
                    const neededLines = (row.order_lines || []).filter(
                      (ol) => ol.status === "needed"
                    );
                    const firstLine = neededLines[0] || row.order_lines?.[0] || null;
                    return (
                      <tr key={row.key} className="hover:bg-stone-50/70">
                        <td className="px-3 py-2.5 font-bold text-stone-900">
                          {row.material_name}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span className="px-2 py-0.5 rounded-full bg-stone-100 text-stone-700 font-semibold">
                            {row.category_label}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap font-extrabold text-amber-950">
                          {fmtQty(row.total_demand_qty)} {row.unit}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          {row.in_stock_qty > 0 ? (
                            <span className="font-bold text-emerald-700">
                              🟢 {fmtQty(row.in_stock_qty)} {row.unit}
                            </span>
                          ) : (
                            <span className="text-stone-400">0</span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 min-w-[200px]">
                          {(row.order_lines || []).length === 0 ? (
                            <span className="text-stone-500 italic">Workshop Restock Alert</span>
                          ) : (
                            <div className="flex flex-wrap gap-1">
                              {row.order_lines.map((ol) => (
                                <button
                                  key={ol.material_id}
                                  type="button"
                                  onClick={() => onOpenOrder?.(ol.project_id)}
                                  className="px-2 py-1 rounded-lg bg-stone-100 hover:bg-amber-100 text-stone-800 text-[11px] font-semibold text-left"
                                >
                                  #{ol.project_code} · {ol.customer_name} ({ol.item_name}:{" "}
                                  {fmtQty(ol.quantity)})
                                </button>
                              ))}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span
                            className={`px-2 py-0.5 rounded-full font-bold text-[11px] ${
                              row.status === "needed"
                                ? "bg-amber-100 text-amber-950"
                                : row.status === "ordered"
                                ? "bg-sky-100 text-sky-900"
                                : "bg-rose-100 text-rose-900"
                            }`}
                          >
                            {row.status === "needed"
                              ? "To Order"
                              : row.status === "ordered"
                              ? "Ordered"
                              : "Low Stock"}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap text-right">
                          <div className="inline-flex items-center gap-1.5">
                            {row.in_stock_qty > 0 &&
                              row.matched_inventory_item_id &&
                              neededLines.length > 0 && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    openAttributeModal({
                                      inventory_item_id: row.matched_inventory_item_id,
                                      project_id: neededLines[0].project_id,
                                      item_id: neededLines[0].item_id || "",
                                      item_material_id: neededLines[0].material_id,
                                      quantity: Math.min(neededLines[0].quantity, row.in_stock_qty),
                                    })
                                  }
                                  className="min-h-[40px] px-2.5 rounded-lg bg-emerald-600 text-white font-bold"
                                >
                                  Pull from Stock
                                </button>
                              )}
                            {row.status === "needed" && neededLines.length > 0 && (
                              <button
                                type="button"
                                disabled={busyKey === `ord-${row.key}`}
                                onClick={() => handleMarkRowOrdered(row)}
                                className="min-h-[40px] px-2.5 rounded-lg border border-sky-200 bg-sky-50 text-sky-900 font-bold"
                              >
                                Mark Ordered
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() =>
                                openLoadStockModal(
                                  row.source === "workshop_restock" || !firstLine
                                    ? {
                                        destination: "stock",
                                        inventory_item_id: row.matched_inventory_item_id || "",
                                        is_new_sku: !row.matched_inventory_item_id,
                                        material_name: row.material_name,
                                        category: row.category,
                                        unit: row.unit,
                                        quantity: String(row.total_demand_qty || 1),
                                      }
                                    : {
                                        destination: "order",
                                        project_id: firstLine.project_id,
                                        item_id: firstLine.item_id || "",
                                        matched_item_material_id: firstLine.material_id,
                                        fulfill_manual: true,
                                        material_name: row.material_name,
                                        category: row.category,
                                        unit: row.unit,
                                        quantity: String(firstLine.quantity || row.total_demand_qty || 1),
                                      }
                                )
                              }
                              className="min-h-[40px] px-2.5 rounded-lg bg-amber-900 text-white font-bold"
                            >
                              Gate-In
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* SUB-TAB 3: STOCK LEDGER */}
      {subTab === "ledger" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="w-4 h-4 text-stone-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                value={ledgerSearch}
                onChange={(e) => setLedgerSearch(e.target.value)}
                placeholder="Search movement, customer, vendor or bill no..."
                className="w-full min-h-[44px] pl-9 pr-3 rounded-xl border border-stone-200 bg-white text-xs"
              />
            </div>
            <button
              type="button"
              onClick={() => setRatePendingOnly((v) => !v)}
              className={`min-h-[44px] px-3.5 rounded-xl border text-xs font-bold ${
                ratePendingOnly
                  ? "bg-rose-700 text-white border-rose-700"
                  : "bg-white text-stone-700 border-stone-200"
              }`}
            >
              Rate Pending Only ({kpis.rate_pending_count})
            </button>
          </div>

          {filteredMovements.length === 0 ? (
            <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center text-xs text-stone-500">
              No stock movements recorded yet.
            </div>
          ) : (
            <div className="bg-white rounded-2xl border border-stone-200 overflow-x-auto shadow-xs">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-stone-200 bg-stone-50 text-stone-600">
                    <th className="px-3 py-2.5 font-bold">Date</th>
                    <th className="px-3 py-2.5 font-bold">Type</th>
                    <th className="px-3 py-2.5 font-bold">Material</th>
                    <th className="px-3 py-2.5 font-bold">Category</th>
                    <th className="px-3 py-2.5 font-bold">Qty Δ</th>
                    <th className="px-3 py-2.5 font-bold">Amount ₹</th>
                    <th className="px-3 py-2.5 font-bold">Order / Party</th>
                    <th className="px-3 py-2.5 font-bold">Zoho</th>
                    <th className="px-3 py-2.5 font-bold text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-100">
                  {filteredMovements.map((mv) => {
                    const isRp = mv.zoho_sync_status === "rate_pending" && !mv.is_undone;
                    return (
                      <tr
                        key={mv.id}
                        className={mv.is_undone ? "opacity-50 line-through bg-stone-50" : "hover:bg-stone-50/70"}
                      >
                        <td className="px-3 py-2.5 whitespace-nowrap text-stone-500">
                          {formatDate(mv.created_at)}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap font-semibold">
                          {MOVEMENT_TYPE_LABELS[mv.movement_type] || mv.movement_type}
                        </td>
                        <td className="px-3 py-2.5 font-bold text-stone-900">
                          {mv.material_label}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap text-stone-600">
                          {mv.category_label}
                        </td>
                        <td
                          className={`px-3 py-2.5 whitespace-nowrap font-extrabold ${
                            Number(mv.qty_delta) >= 0 ? "text-emerald-700" : "text-amber-800"
                          }`}
                        >
                          {Number(mv.qty_delta) >= 0 ? "+" : ""}
                          {fmtQty(mv.qty_delta)} {mv.unit}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          {mv.total_inr != null ? (
                            <span className="font-bold text-stone-900">
                              {formatINR(mv.total_inr)}
                            </span>
                          ) : (
                            <span className="text-rose-700 font-bold">Rate Pending</span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-stone-600">
                          {mv.customer_name && (
                            <button
                              type="button"
                              onClick={() => mv.project_id && onOpenOrder?.(mv.project_id)}
                              className="font-semibold text-amber-900 hover:underline mr-1"
                            >
                              #{mv.project_code} {mv.customer_name}
                            </button>
                          )}
                          {mv.vendor_name && <span>{mv.vendor_name}</span>}
                          {mv.account_name && <span> • {mv.account_name}</span>}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span
                            className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${
                              isRp
                                ? "bg-rose-100 text-rose-800"
                                : mv.zoho_sync_status === "synced"
                                ? "bg-emerald-100 text-emerald-800"
                                : "bg-stone-100 text-stone-600"
                            }`}
                          >
                            {isRp ? "Rate Pending" : mv.zoho_sync_status}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap text-right">
                          <div className="inline-flex items-center gap-1.5">
                            {isRp && (
                              <button
                                type="button"
                                onClick={() => {
                                  setModalErr("");
                                  setRateModal({
                                    mv,
                                    settlement_mode: "paid_now",
                                    unit_rate_inr: "",
                                    payment_account_id: accounts[0]?.id || "",
                                    vendor_id: mv.vendor_id || "",
                                    vendor_name: "",
                                    invoice_no: mv.invoice_no || "",
                                    notes: mv.notes || "",
                                  });
                                }}
                                className="min-h-[40px] px-2.5 rounded-lg bg-rose-700 text-white font-bold flex items-center gap-1"
                              >
                                <IndianRupee className="w-3.5 h-3.5" />
                                <span>Enter Rate</span>
                              </button>
                            )}
                            {mv.can_undo && (
                              <button
                                type="button"
                                disabled={busyKey === `undo-${mv.id}`}
                                onClick={() => handleUndoMovement(mv)}
                                className="min-h-[40px] px-2.5 rounded-lg border border-stone-300 bg-white text-stone-700 font-semibold flex items-center gap-1"
                              >
                                <Undo2 className="w-3.5 h-3.5" />
                                <span>Undo</span>
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* MODAL 1: LOAD STOCK / GATE-IN */}
      {loadModal && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-2 sm:p-4">
          <form
            onSubmit={handleSubmitLoad}
            className="bg-white w-full max-w-2xl rounded-2xl border border-stone-200 shadow-2xl max-h-[90vh] flex flex-col overflow-hidden"
          >
            <div className="px-4 py-3 border-b border-stone-200 flex items-center justify-between">
              <h3 className="font-extrabold text-sm text-stone-900">
                Load Stock / Gate-In Delivery
              </h3>
              <button
                type="button"
                onClick={() => setLoadModal(null)}
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-4 overflow-y-auto space-y-3 text-xs">
              {modalErr && (
                <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 font-semibold">
                  {modalErr}
                </div>
              )}

              {/* Settlement Mode Tabs */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
                {[
                  { id: "paid_now", label: "💳 Paid Now" },
                  { id: "vendor_udhaar", label: "📝 Vendor Udhaar" },
                  { id: "manual", label: "🏗️ Opening / Manual" },
                  { id: "rate_pending", label: "⏳ Rate Pending" },
                ].map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() =>
                      setLoadModal((prev) => ({ ...prev, settlement_mode: m.id }))
                    }
                    className={`min-h-[42px] px-2 rounded-xl border font-bold ${
                      loadModal.settlement_mode === m.id
                        ? "bg-amber-900 text-white border-amber-900"
                        : "bg-stone-50 text-stone-700 border-stone-200"
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>

              {/* Settlement Header Fields */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {loadModal.settlement_mode === "paid_now" && (
                  <select
                    value={loadModal.payment_account_id}
                    onChange={(e) =>
                      setLoadModal((prev) => ({
                        ...prev,
                        payment_account_id: e.target.value,
                      }))
                    }
                    className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
                  >
                    <option value="">Select Payment Account...</option>
                    {accounts.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.display_name}
                      </option>
                    ))}
                  </select>
                )}

                {(loadModal.settlement_mode === "paid_now" ||
                  loadModal.settlement_mode === "vendor_udhaar" ||
                  loadModal.settlement_mode === "rate_pending") && (
                  <>
                    <select
                      value={loadModal.vendor_id}
                      onChange={(e) =>
                        setLoadModal((prev) => ({ ...prev, vendor_id: e.target.value }))
                      }
                      className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
                    >
                      <option value="">
                        {loadModal.settlement_mode === "vendor_udhaar"
                          ? "Select Supplier (required)..."
                          : "Supplier (optional / type next)..."}
                      </option>
                      {vendors.map((v) => (
                        <option key={v.id} value={v.id}>
                          {v.canonical_name}
                        </option>
                      ))}
                    </select>
                    {!loadModal.vendor_id && (
                      <input
                        value={loadModal.vendor_name}
                        onChange={(e) =>
                          setLoadModal((prev) => ({
                            ...prev,
                            vendor_name: e.target.value,
                          }))
                        }
                        placeholder="Or type supplier name"
                        className="min-h-[44px] px-2.5 rounded-xl border border-stone-300"
                      />
                    )}
                    <input
                      value={loadModal.invoice_no}
                      onChange={(e) =>
                        setLoadModal((prev) => ({ ...prev, invoice_no: e.target.value }))
                      }
                      placeholder="Bill / Challan No."
                      className="min-h-[44px] px-2.5 rounded-xl border border-stone-300"
                    />
                  </>
                )}
              </div>

              {/* Lines */}
              <div className="space-y-2.5">
                {loadModal.lines.map((ln, idx) => {
                  const selectedOrder = activeOrders.find((o) => o.id === ln.project_id);
                  const orderOpenMats = openOrderMaterialsByProject[ln.project_id] || [];
                  return (
                    <div
                      key={ln.key}
                      className="rounded-xl border border-stone-200 bg-stone-50/70 p-3 space-y-2"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => updateLoadLine(idx, { destination: "stock" })}
                            className={`min-h-[38px] px-3 rounded-lg font-bold ${
                              ln.destination === "stock"
                                ? "bg-stone-900 text-white"
                                : "bg-white border border-stone-200 text-stone-700"
                            }`}
                          >
                            🏪 Workshop Stock
                          </button>
                          <button
                            type="button"
                            onClick={() =>
                              updateLoadLine(idx, {
                                destination: "order",
                                project_id: ln.project_id || activeOrders[0]?.id || "",
                              })
                            }
                            className={`min-h-[38px] px-3 rounded-lg font-bold ${
                              ln.destination === "order"
                                ? "bg-amber-900 text-white"
                                : "bg-white border border-stone-200 text-stone-700"
                            }`}
                          >
                            👤 Customer Order
                          </button>
                        </div>
                        {loadModal.lines.length > 1 && (
                          <button
                            type="button"
                            onClick={() =>
                              setLoadModal((prev) => ({
                                ...prev,
                                lines: prev.lines.filter((_, i) => i !== idx),
                              }))
                            }
                            className="text-rose-600 font-bold px-2 py-1"
                          >
                            Remove
                          </button>
                        )}
                      </div>

                      {ln.destination === "stock" ? (
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                          <select
                            value={ln.is_new_sku ? "__new__" : ln.inventory_item_id}
                            onChange={(e) => {
                              const val = e.target.value;
                              if (val === "__new__" || !val) {
                                updateLoadLine(idx, {
                                  is_new_sku: val === "__new__",
                                  inventory_item_id: "",
                                });
                              } else {
                                const hit = items.find((i) => i.id === val);
                                updateLoadLine(idx, {
                                  is_new_sku: false,
                                  inventory_item_id: val,
                                  material_name: hit?.name || "",
                                  category: hit?.category || "ply_wood",
                                  unit: hit?.unit || "pcs",
                                });
                              }
                            }}
                            className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
                          >
                            <option value="">Select Stock SKU or create new...</option>
                            {items.map((it) => (
                              <option key={it.id} value={it.id}>
                                {it.name} ({fmtQty(it.on_hand_qty)} {it.unit})
                              </option>
                            ))}
                            <option value="__new__">➕ Create New Stock SKU inline</option>
                          </select>

                          {(ln.is_new_sku || !ln.inventory_item_id) && (
                            <input
                              value={ln.material_name}
                              onChange={(e) =>
                                updateLoadLine(idx, {
                                  is_new_sku: true,
                                  material_name: e.target.value,
                                })
                              }
                              placeholder="New SKU Name (e.g. 18mm Century Ply)"
                              className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                            />
                          )}
                        </div>
                      ) : (
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                          <select
                            value={ln.project_id}
                            onChange={(e) =>
                              updateLoadLine(idx, {
                                project_id: e.target.value,
                                item_id: "",
                                matched_item_material_id: "",
                                fulfill_manual: false,
                              })
                            }
                            className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
                          >
                            <option value="">Select Customer Order...</option>
                            {activeOrders.map((o) => (
                              <option key={o.id} value={o.id}>
                                #{o.project_code} · {o.customer_name}
                              </option>
                            ))}
                          </select>

                          <input
                            value={ln.material_name}
                            onChange={(e) =>
                              updateLoadLine(idx, {
                                material_name: e.target.value,
                                fulfill_manual: false,
                              })
                            }
                            placeholder="Received Material (e.g. Walnut Charcoal Louver)"
                            className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                          />

                          <select
                            value={ln.matched_item_material_id}
                            onChange={(e) => {
                              const mid = e.target.value;
                              const matchOl = orderOpenMats.find((ol) => ol.material_id === mid);
                              updateLoadLine(idx, {
                                matched_item_material_id: mid,
                                fulfill_manual: true,
                                ...(matchOl ? { item_id: matchOl.item_id || "" } : {}),
                              });
                            }}
                            className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                          >
                            <option value="">
                              ➕ New / Unlisted Material (Auto-add In Shop)
                            </option>
                            {orderOpenMats.map((ol) => (
                              <option key={ol.material_id} value={ol.material_id}>
                                Fulfills: {ol.material_name} ({fmtQty(ol.quantity)} {ol.unit} ·{" "}
                                {ol.item_name})
                              </option>
                            ))}
                          </select>

                          {!ln.matched_item_material_id && (
                            <select
                              value={ln.item_id}
                              onChange={(e) => updateLoadLine(idx, { item_id: e.target.value })}
                              className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                            >
                              <option value="">Order-Wide (no specific item)</option>
                              {(selectedOrder?.items || []).map((oi) => (
                                <option key={oi.id} value={oi.id}>
                                  Item: {oi.item_name}
                                </option>
                              ))}
                            </select>
                          )}
                        </div>
                      )}

                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                        <input
                          type="number"
                          min="0.01"
                          step="any"
                          value={ln.quantity}
                          onChange={(e) => updateLoadLine(idx, { quantity: e.target.value })}
                          placeholder="Qty"
                          className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-bold"
                        />
                        <select
                          value={ln.unit}
                          onChange={(e) => updateLoadLine(idx, { unit: e.target.value })}
                          className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                        >
                          {UNIT_OPTIONS.map((u) => (
                            <option key={u} value={u}>
                              {u}
                            </option>
                          ))}
                        </select>
                        <select
                          value={ln.category}
                          onChange={(e) => updateLoadLine(idx, { category: e.target.value })}
                          className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                        >
                          {categories.map((c) => (
                            <option key={c.code} value={c.code}>
                              {c.label}
                            </option>
                          ))}
                        </select>
                        {loadModal.settlement_mode !== "rate_pending" ? (
                          <input
                            type="number"
                            min="0"
                            step="any"
                            required
                            value={ln.unit_rate_inr}
                            onChange={(e) =>
                              updateLoadLine(idx, { unit_rate_inr: e.target.value })
                            }
                            placeholder="Rate ₹ / unit"
                            className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-bold"
                          />
                        ) : (
                          <div className="min-h-[44px] px-2.5 rounded-xl bg-amber-50 border border-amber-200 flex items-center text-[11px] font-semibold text-amber-900">
                            Rate Pending
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
                <button
                  type="button"
                  onClick={() =>
                    setLoadModal((prev) => ({
                      ...prev,
                      lines: [...prev.lines, emptyLoadLine("stock")],
                    }))
                  }
                  className="min-h-[40px] px-3 rounded-xl border border-dashed border-stone-300 text-stone-700 font-bold w-full"
                >
                  + Add Another Line (Mix Stock &amp; Customer Items)
                </button>
              </div>

              <input
                value={loadModal.notes}
                onChange={(e) =>
                  setLoadModal((prev) => ({ ...prev, notes: e.target.value }))
                }
                placeholder="Notes (optional)"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300"
              />
            </div>

            <div className="p-3 border-t border-stone-200 flex justify-end gap-2 bg-stone-50">
              <button
                type="button"
                onClick={() => setLoadModal(null)}
                className="min-h-[44px] px-4 rounded-xl border border-stone-300 bg-white text-xs font-semibold"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busyKey === "submit-load"}
                className="min-h-[44px] px-5 rounded-xl bg-amber-900 text-white text-xs font-bold"
              >
                {busyKey === "submit-load" ? "Saving..." : "Confirm & Post"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* MODAL 2: ATTRIBUTE STOCK TO CUSTOMER ORDER */}
      {attrModal && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-2 sm:p-4">
          <form
            onSubmit={handleSubmitAttribute}
            className="bg-white w-full max-w-md rounded-2xl border border-stone-200 shadow-2xl overflow-hidden text-xs"
          >
            <div className="px-4 py-3 border-b border-stone-200 flex items-center justify-between">
              <h3 className="font-extrabold text-sm text-stone-900">
                Attribute Stock → Customer Order
              </h3>
              <button
                type="button"
                onClick={() => setAttrModal(null)}
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-4 space-y-3">
              {modalErr && (
                <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 font-semibold">
                  {modalErr}
                </div>
              )}

              <select
                value={attrModal.inventory_item_id}
                onChange={(e) =>
                  setAttrModal((prev) => ({
                    ...prev,
                    inventory_item_id: e.target.value,
                  }))
                }
                className="w-full min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
              >
                <option value="">Select SKU...</option>
                {items
                  .filter((i) => Number(i.on_hand_qty || 0) > 0)
                  .map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.name} ({fmtQty(i.on_hand_qty)} {i.unit} @ {formatINR(i.avg_unit_cost_inr)})
                    </option>
                  ))}
              </select>

              <select
                value={attrModal.project_id}
                onChange={(e) =>
                  setAttrModal((prev) => ({
                    ...prev,
                    project_id: e.target.value,
                    item_id: "",
                    item_material_id: "",
                  }))
                }
                className="w-full min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
              >
                <option value="">Select Customer Order...</option>
                {activeOrders.map((o) => (
                  <option key={o.id} value={o.id}>
                    #{o.project_code} · {o.customer_name}
                  </option>
                ))}
              </select>

              <select
                value={attrModal.item_id}
                onChange={(e) =>
                  setAttrModal((prev) => ({ ...prev, item_id: e.target.value }))
                }
                className="w-full min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
              >
                <option value="">Order-Wide (no specific furniture item)</option>
                {(
                  activeOrders.find((o) => o.id === attrModal.project_id)?.items || []
                ).map((oi) => (
                  <option key={oi.id} value={oi.id}>
                    Item: {oi.item_name}
                  </option>
                ))}
              </select>

              {(openOrderMaterialsByProject[attrModal.project_id] || []).length > 0 && (
                <select
                  value={attrModal.item_material_id}
                  onChange={(e) => {
                    const mid = e.target.value;
                    const ol = (
                      openOrderMaterialsByProject[attrModal.project_id] || []
                    ).find((r) => r.material_id === mid);
                    setAttrModal((prev) => ({
                      ...prev,
                      item_material_id: mid,
                      ...(ol ? { item_id: ol.item_id || "" } : {}),
                    }));
                  }}
                  className="w-full min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                >
                  <option value="">
                    Fulfills Open Line: Auto-match or Add New
                  </option>
                  {(openOrderMaterialsByProject[attrModal.project_id] || []).map(
                    (ol) => (
                      <option key={ol.material_id} value={ol.material_id}>
                        Fulfills: {ol.material_name} ({fmtQty(ol.quantity)}{" "}
                        {ol.unit} · {ol.item_name})
                      </option>
                    )
                  )}
                </select>
              )}

              <input
                type="number"
                min="0.01"
                max={
                  items.find((i) => i.id === attrModal.inventory_item_id)?.on_hand_qty ||
                  undefined
                }
                step="any"
                required
                value={attrModal.quantity}
                onChange={(e) =>
                  setAttrModal((prev) => ({ ...prev, quantity: e.target.value }))
                }
                placeholder="Quantity to Pull"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 font-bold"
              />

              {(() => {
                const s = items.find((i) => i.id === attrModal.inventory_item_id);
                if (!s) return null;
                const estCogs = Number(attrModal.quantity || 0) * Number(s.avg_unit_cost_inr || 0);
                return (
                  <div className="text-[11px] text-stone-600 flex items-center justify-between px-1">
                    <span>
                      Available: <b>{fmtQty(s.on_hand_qty)} {s.unit}</b>
                    </span>
                    <span>
                      Est. Zoho COGS: <b>{formatINR(estCogs)}</b>
                    </span>
                  </div>
                );
              })()}

              <input
                value={attrModal.notes}
                onChange={(e) =>
                  setAttrModal((prev) => ({ ...prev, notes: e.target.value }))
                }
                placeholder="Notes (optional)"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300"
              />
            </div>

            <div className="p-3 border-t border-stone-200 flex justify-end gap-2 bg-stone-50">
              <button
                type="button"
                onClick={() => setAttrModal(null)}
                className="min-h-[44px] px-4 rounded-xl border border-stone-300 bg-white font-semibold"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busyKey === "submit-attr"}
                className="min-h-[44px] px-5 rounded-xl bg-emerald-700 text-white font-bold"
              >
                {busyKey === "submit-attr" ? "Pulling..." : "Pull & Sync Zoho COGS"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* MODAL 3: PHYSICAL COUNT ADJUST */}
      {countModal && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-2 sm:p-4">
          <form
            onSubmit={handleSubmitCountAdjust}
            className="bg-white w-full max-w-sm rounded-2xl border border-stone-200 shadow-2xl overflow-hidden text-xs"
          >
            <div className="px-4 py-3 border-b border-stone-200 flex items-center justify-between">
              <h3 className="font-extrabold text-sm text-stone-900 flex items-center gap-1.5">
                <SlidersHorizontal className="w-4 h-4 text-amber-800" />
                <span>Physical Count Adjust</span>
              </h3>
              <button
                type="button"
                onClick={() => setCountModal(null)}
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              {modalErr && (
                <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 font-semibold">
                  {modalErr}
                </div>
              )}
              <div className="p-2.5 rounded-xl bg-stone-50 border border-stone-200">
                <div className="font-bold text-stone-900">{countModal.sku.name}</div>
                <div className="text-stone-600 mt-0.5">
                  Current Book Qty: <b>{fmtQty(countModal.sku.on_hand_qty)} {countModal.sku.unit}</b>
                </div>
              </div>
              <input
                type="number"
                min="0"
                step="any"
                value={countModal.actual_qty}
                onChange={(e) =>
                  setCountModal((prev) => ({ ...prev, actual_qty: e.target.value }))
                }
                placeholder="Actual Physical Qty in Godown"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 font-bold"
              />
              <input
                value={countModal.notes}
                onChange={(e) =>
                  setCountModal((prev) => ({ ...prev, notes: e.target.value }))
                }
                placeholder="Reason (e.g. shrinkage, floor consumable)"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300"
              />
            </div>
            <div className="p-3 border-t border-stone-200 flex justify-end gap-2 bg-stone-50">
              <button
                type="button"
                onClick={() => setCountModal(null)}
                className="min-h-[44px] px-4 rounded-xl border border-stone-300 bg-white font-semibold"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busyKey === "submit-count"}
                className="min-h-[44px] px-4 rounded-xl bg-stone-900 text-white font-bold"
              >
                {busyKey === "submit-count" ? "Posting..." : "Update & Post COGS"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* MODAL 4: ADD / EDIT SKU */}
      {skuModal && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-2 sm:p-4">
          <form
            onSubmit={handleSubmitSku}
            className="bg-white w-full max-w-md rounded-2xl border border-stone-200 shadow-2xl overflow-hidden text-xs"
          >
            <div className="px-4 py-3 border-b border-stone-200 flex items-center justify-between">
              <h3 className="font-extrabold text-sm text-stone-900">
                {skuModal.sku_id ? "Edit Stock SKU" : "Add Standing Stock SKU"}
              </h3>
              <button
                type="button"
                onClick={() => setSkuModal(null)}
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 space-y-2.5">
              {modalErr && (
                <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 font-semibold">
                  {modalErr}
                </div>
              )}
              <input
                value={skuModal.name}
                onChange={(e) =>
                  setSkuModal((prev) => ({ ...prev, name: e.target.value }))
                }
                placeholder="SKU Name (e.g. 18mm HDHMR Ply, Ebco Soft Close Hinges)"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 font-semibold"
                required
              />
              <div className="grid grid-cols-2 gap-2">
                <select
                  value={skuModal.category}
                  onChange={(e) =>
                    setSkuModal((prev) => ({ ...prev, category: e.target.value }))
                  }
                  className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                >
                  {categories.map((c) => (
                    <option key={c.code} value={c.code}>
                      {c.label}
                    </option>
                  ))}
                </select>
                <select
                  value={skuModal.unit}
                  onChange={(e) =>
                    setSkuModal((prev) => ({ ...prev, unit: e.target.value }))
                  }
                  className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                >
                  {UNIT_OPTIONS.map((u) => (
                    <option key={u} value={u}>
                      {u}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={skuModal.min_stock_qty}
                  onChange={(e) =>
                    setSkuModal((prev) => ({ ...prev, min_stock_qty: e.target.value }))
                  }
                  placeholder="Min Alert Qty"
                  className="min-h-[44px] px-3 rounded-xl border border-stone-300"
                />
                {!skuModal.sku_id && (
                  <input
                    type="number"
                    min="0"
                    step="any"
                    value={skuModal.avg_unit_cost_inr}
                    onChange={(e) =>
                      setSkuModal((prev) => ({
                        ...prev,
                        avg_unit_cost_inr: e.target.value,
                      }))
                    }
                    placeholder="Default Cost ₹ (optional)"
                    className="min-h-[44px] px-3 rounded-xl border border-stone-300"
                  />
                )}
              </div>
              <input
                value={skuModal.aliasesText}
                onChange={(e) =>
                  setSkuModal((prev) => ({ ...prev, aliasesText: e.target.value }))
                }
                placeholder="Hinglish aliases CSV (e.g. kabje, soft close)"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300"
              />
            </div>
            <div className="p-3 border-t border-stone-200 flex justify-end gap-2 bg-stone-50">
              <button
                type="button"
                onClick={() => setSkuModal(null)}
                className="min-h-[44px] px-4 rounded-xl border border-stone-300 bg-white font-semibold"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busyKey === "submit-sku"}
                className="min-h-[44px] px-5 rounded-xl bg-amber-900 text-white font-bold"
              >
                {busyKey === "submit-sku" ? "Saving..." : "Save SKU"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* MODAL 5: ENTER RATE ON RATE-PENDING RECEIPT */}
      {rateModal && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-2 sm:p-4">
          <form
            onSubmit={handleSubmitRate}
            className="bg-white w-full max-w-md rounded-2xl border border-stone-200 shadow-2xl overflow-hidden text-xs"
          >
            <div className="px-4 py-3 border-b border-stone-200 flex items-center justify-between">
              <h3 className="font-extrabold text-sm text-stone-900 flex items-center gap-1.5">
                <ClipboardCheck className="w-4 h-4 text-rose-700" />
                <span>Enter Rate (Sync to Zoho)</span>
              </h3>
              <button
                type="button"
                onClick={() => setRateModal(null)}
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              {modalErr && (
                <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 font-semibold">
                  {modalErr}
                </div>
              )}
              <div className="p-2.5 rounded-xl bg-stone-50 border border-stone-200">
                <div className="font-bold text-stone-900">{rateModal.mv.material_label}</div>
                <div className="text-stone-600">
                  Qty already gated in: <b>{fmtQty(Math.abs(rateModal.mv.qty_delta))} {rateModal.mv.unit}</b>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-1.5">
                {[
                  { id: "paid_now", label: "Paid Now" },
                  { id: "vendor_udhaar", label: "Vendor Udhaar" },
                  { id: "manual", label: "Opening / Manual" },
                ].map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() =>
                      setRateModal((prev) => ({ ...prev, settlement_mode: m.id }))
                    }
                    className={`min-h-[40px] rounded-xl border font-bold ${
                      rateModal.settlement_mode === m.id
                        ? "bg-amber-900 text-white border-amber-900"
                        : "bg-white text-stone-700 border-stone-200"
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>

              <input
                type="number"
                min="0.01"
                step="any"
                required
                value={rateModal.unit_rate_inr}
                onChange={(e) =>
                  setRateModal((prev) => ({ ...prev, unit_rate_inr: e.target.value }))
                }
                placeholder="Unit Rate ₹"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 font-bold"
              />

              {rateModal.settlement_mode === "paid_now" && (
                <select
                  value={rateModal.payment_account_id}
                  onChange={(e) =>
                    setRateModal((prev) => ({
                      ...prev,
                      payment_account_id: e.target.value,
                    }))
                  }
                  className="w-full min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white font-semibold"
                >
                  <option value="">Select Payment Account...</option>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.display_name}
                    </option>
                  ))}
                </select>
              )}

              {(rateModal.settlement_mode === "vendor_udhaar" ||
                rateModal.settlement_mode === "paid_now") && (
                <div className="space-y-2">
                  <div className="grid grid-cols-2 gap-2">
                    <select
                      value={rateModal.vendor_id}
                      onChange={(e) =>
                        setRateModal((prev) => ({ ...prev, vendor_id: e.target.value }))
                      }
                      className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 bg-white"
                    >
                      <option value="">Select Supplier...</option>
                      {vendors.map((v) => (
                        <option key={v.id} value={v.id}>
                          {v.canonical_name}
                        </option>
                      ))}
                    </select>
                    <input
                      value={rateModal.invoice_no}
                      onChange={(e) =>
                        setRateModal((prev) => ({ ...prev, invoice_no: e.target.value }))
                      }
                      placeholder="Bill / Invoice No."
                      className="min-h-[44px] px-2.5 rounded-xl border border-stone-300"
                    />
                  </div>
                  {!rateModal.vendor_id && (
                    <input
                      value={rateModal.vendor_name}
                      onChange={(e) =>
                        setRateModal((prev) => ({ ...prev, vendor_name: e.target.value }))
                      }
                      placeholder="Or type supplier name"
                      className="w-full min-h-[44px] px-2.5 rounded-xl border border-stone-300"
                    />
                  )}
                </div>
              )}
            </div>
            <div className="p-3 border-t border-stone-200 flex justify-end gap-2 bg-stone-50">
              <button
                type="button"
                onClick={() => setRateModal(null)}
                className="min-h-[44px] px-4 rounded-xl border border-stone-300 bg-white font-semibold"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busyKey === "submit-rate"}
                className="min-h-[44px] px-5 rounded-xl bg-rose-700 text-white font-bold flex items-center gap-1"
              >
                <CheckCircle2 className="w-4 h-4" />
                <span>{busyKey === "submit-rate" ? "Syncing..." : "Save & Sync Zoho"}</span>
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
