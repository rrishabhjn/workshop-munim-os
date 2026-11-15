import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { apiFetch } from "../lib/api";

export default function PurchaseList() {
  const [rows, setRows] = useState([]);
  const [expanded, setExpanded] = useState({});

  async function loadList() {
    const data = await apiFetch("/api/purchase-list");
    setRows(data);
  }

  useEffect(() => {
    loadList();
  }, []);

  async function markOrdered(materialId, nextStatus) {
    await apiFetch(`/api/materials/${materialId}`, {
      method: "PATCH",
      body: JSON.stringify({ status: nextStatus }),
    });
    await loadList();
  }

  // Group rows by category_label
  const grouped = rows.reduce((acc, row) => {
    const key = row.category_label || row.category;
    acc[key] = acc[key] || [];
    acc[key].push(row);
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      <div className="bg-amber-50 border border-amber-200 rounded-2xl p-3 text-xs text-amber-950">
        Aggregated across all <strong>active</strong> customer orders where material status is{" "}
        <strong>Needed</strong>. Tap a row to expand customer breakdowns and mark{" "}
        <strong>Ordered</strong> or <strong>In Shop</strong>.
      </div>

      {Object.keys(grouped).length === 0 ? (
        <div className="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">
          All materials are ordered or in shop!
        </div>
      ) : (
        Object.entries(grouped).map(([categoryLabel, items]) => (
          <div key={categoryLabel} className="space-y-2">
            <h3 className="text-sm font-bold uppercase tracking-wide text-stone-600 px-1">
              {categoryLabel}
            </h3>
            {items.map((row) => {
              const key = `${row.category}:${row.material_name}:${row.unit}`;
              const isOpen = !!expanded[key];
              return (
                <div
                  key={key}
                  className="bg-white rounded-2xl border border-stone-200 overflow-hidden"
                >
                  <button
                    onClick={() =>
                      setExpanded({ ...expanded, [key]: !isOpen })
                    }
                    className="w-full px-4 py-3 flex items-center justify-between text-left"
                  >
                    <div className="flex items-center gap-2">
                      {isOpen ? (
                        <ChevronDown className="w-4 h-4 text-stone-400" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-stone-400" />
                      )}
                      <div>
                        <div className="font-bold text-stone-900">
                          {row.material_name}
                        </div>
                        <div className="text-xs text-stone-500">
                          Across {row.item_count} furniture piece(s)
                        </div>
                      </div>
                    </div>
                    <div className="text-right">
                      <span className="text-base font-extrabold text-amber-900">
                        {row.total_qty_needed} {row.unit}
                      </span>
                    </div>
                  </button>

                  {isOpen && (
                    <div className="border-t border-stone-100 bg-stone-50 px-4 py-2 space-y-2">
                      {(row.customer_breakdown || []).map((b) => (
                        <div
                          key={b.material_id}
                          className="flex flex-wrap items-center justify-between gap-2 text-sm py-1"
                        >
                          <div>
                            <span className="font-semibold">{b.customer_name}</span> —{" "}
                            {b.item_name} ({b.quantity} {row.unit})
                          </div>
                          <div className="flex gap-1.5">
                            <button
                              onClick={() => markOrdered(b.material_id, "ordered")}
                              className="px-3 py-1.5 rounded-lg bg-amber-100 text-amber-950 text-xs font-semibold"
                            >
                              Ordered
                            </button>
                            <button
                              onClick={() => markOrdered(b.material_id, "in_shop")}
                              className="px-3 py-1.5 rounded-lg bg-emerald-100 text-emerald-900 text-xs font-semibold"
                            >
                              In Shop
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ))
      )}
    </div>
  );
}
