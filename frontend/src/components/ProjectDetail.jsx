import { useEffect, useState } from "react";
import { ArrowLeft, Camera, Plus, Share2, CheckSquare, Square } from "lucide-react";
import { apiFetch } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import { shareKarigarJobCard } from "../lib/jobCard";

export default function ProjectDetail({ projectId, stages, categories, onBack, onRefresh }) {
  const [project, setProject] = useState(null);
  const [signedUrls, setSignedUrls] = useState({});
  const [newItemName, setNewItemName] = useState("");
  const [newPartByItem, setNewPartByItem] = useState({});
  const [newMatByItem, setNewMatByItem] = useState({});
  const [warningMsg, setWarningMsg] = useState("");

  async function loadDetail() {
    const data = await apiFetch(`/api/projects/${projectId}`);
    setProject(data);
    // Fetch signed URLs for item photos
    const paths = (data.items || []).flatMap((it) => it.photo_paths || []);
    const entries = await Promise.all(
      paths.map(async (p) => {
        try {
          const res = await apiFetch(`/api/media/signed-url?path=${encodeURIComponent(p)}`);
          return [p, res.url];
        } catch {
          return [p, null];
        }
      })
    );
    setSignedUrls(Object.fromEntries(entries));
  }

  useEffect(() => {
    loadDetail();
  }, [projectId]);

  if (!project) {
    return <div className="p-6 text-stone-500">Loading customer workspace...</div>;
  }

  async function handleAddItem(e) {
    e.preventDefault();
    if (!newItemName.trim()) return;
    await apiFetch(`/api/projects/${projectId}/items`, {
      method: "POST",
      body: JSON.stringify({ item_name: newItemName.trim() }),
    });
    setNewItemName("");
    await loadDetail();
    onRefresh();
  }

  async function handleStageChange(itemId, stage) {
    const res = await apiFetch(`/api/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify({ stage }),
    });
    if (res.needed_materials_warning_count > 0) {
      setWarningMsg(
        `Note: ${res.needed_materials_warning_count} material(s) on this item are still marked "Needed".`
      );
    } else {
      setWarningMsg("");
    }
    await loadDetail();
    onRefresh();
  }

  async function handlePhotoUpload(itemId, file) {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    await apiFetch(`/api/items/${itemId}/photos`, { method: "POST", body: fd });
    await loadDetail();
  }

  async function handleAddPart(itemId) {
    const partName = (newPartByItem[itemId] || "").trim();
    if (!partName) return;
    await apiFetch(`/api/items/${itemId}/parts`, {
      method: "POST",
      body: JSON.stringify({ part_name: partName }),
    });
    setNewPartByItem({ ...newPartByItem, [itemId]: "" });
    await loadDetail();
  }

  async function handleTogglePart(part) {
    await apiFetch(`/api/parts/${part.id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_done: !part.is_done }),
    });
    await loadDetail();
  }

  async function handleAddMaterial(itemId) {
    const draft = newMatByItem[itemId] || {};
    if (!draft.material_name?.trim()) return;
    await apiFetch(`/api/items/${itemId}/materials`, {
      method: "POST",
      body: JSON.stringify({
        category: draft.category || "ply_wood",
        material_name: draft.material_name.trim(),
        quantity: Number(draft.quantity || 1),
        unit: draft.unit || "pcs",
      }),
    });
    setNewMatByItem({ ...newMatByItem, [itemId]: {} });
    await loadDetail();
  }

  async function handleCycleMaterialStatus(mat) {
    const next =
      mat.status === "needed"
        ? "ordered"
        : mat.status === "ordered"
        ? "in_shop"
        : "needed";
    await apiFetch(`/api/materials/${mat.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: next }),
    });
    await loadDetail();
  }

  return (
    <div className="space-y-4">
      <button
        onClick={onBack}
        className="inline-flex items-center gap-2 text-sm font-semibold text-amber-900 px-3 py-2 rounded-xl bg-white border border-stone-200"
      >
        <ArrowLeft className="w-4 h-4" /> Back to Projects
      </button>

      {warningMsg && (
        <div className="bg-amber-50 border border-amber-300 text-amber-900 px-4 py-3 rounded-xl text-sm font-medium">
          {warningMsg}
        </div>
      )}

      {/* Header & SQL P&L */}
      <div className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3">
        <div className="flex flex-wrap justify-between gap-2">
          <div>
            <span className="text-xs font-mono text-stone-500">{project.project_code}</span>
            <h2 className="text-xl font-bold">{project.customer_name}</h2>
          </div>
          <div className="text-right text-sm">
            <div className="text-stone-500">Promised Delivery</div>
            <div className="font-semibold">{formatDate(project.promised_delivery_date)}</div>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-2 border-t border-stone-100 text-sm">
          <div>
            <div className="text-stone-500 text-xs">Quoted</div>
            <div className="font-bold">{formatINR(project.quoted_total)}</div>
          </div>
          <div>
            <div className="text-stone-500 text-xs">Received (Zoho + Cash)</div>
            <div className="font-bold text-emerald-700">{formatINR(project.total_revenue)}</div>
          </div>
          <div>
            <div className="text-stone-500 text-xs">Cost (Bills + Cash)</div>
            <div className="font-bold text-rose-700">{formatINR(project.total_cost)}</div>
          </div>
          <div>
            <div className="text-stone-500 text-xs">Net Profit ({project.margin_pct}%)</div>
            <div className="font-bold">{formatINR(project.net_profit)}</div>
          </div>
        </div>
      </div>

      {/* Add Item */}
      <form onSubmit={handleAddItem} className="flex gap-2">
        <input
          placeholder="Add furniture item (e.g. Teak Bed, 6-Door Almirah)"
          value={newItemName}
          onChange={(e) => setNewItemName(e.target.value)}
          className="flex-1 bg-white border border-stone-300 rounded-xl px-3 py-2.5 text-sm"
        />
        <button
          type="submit"
          className="px-4 py-2.5 rounded-xl bg-amber-800 text-white text-sm font-semibold inline-flex items-center gap-1"
        >
          <Plus className="w-4 h-4" /> Add Piece
        </button>
      </form>

      {/* Furniture Pieces */}
      <div className="space-y-4">
        {(project.items || []).map((it) => {
          const matDraft = newMatByItem[it.id] || {};
          return (
            <div
              key={it.id}
              className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h3 className="text-lg font-bold">{it.item_name}</h3>
                  <p className="text-xs text-stone-500">
                    Karigar: {it.assigned_karigar || "Unassigned"} • Dim:{" "}
                    {it.dimensions || "—"}
                  </p>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={it.stage}
                    onChange={(e) => handleStageChange(it.id, e.target.value)}
                    className="border border-stone-300 rounded-xl px-3 py-2 text-sm bg-stone-50 font-medium"
                  >
                    {stages.map((s) => (
                      <option key={s.code} value={s.code}>
                        {s.label}
                      </option>
                    ))}
                  </select>

                  <button
                    onClick={() =>
                      shareKarigarJobCard({
                        projectCode: project.project_code,
                        item: it,
                      })
                    }
                    className="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-emerald-700 text-white text-sm font-semibold"
                  >
                    <Share2 className="w-4 h-4" /> Share Karigar Card
                  </button>
                </div>
              </div>

              {/* Photos */}
              <div className="flex flex-wrap items-center gap-2">
                {(it.photo_paths || []).map((path) =>
                  signedUrls[path] ? (
                    <img
                      key={path}
                      src={signedUrls[path]}
                      alt={it.item_name}
                      className="w-20 h-20 object-cover rounded-xl border border-stone-200"
                    />
                  ) : null
                )}
                <label className="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl border border-dashed border-stone-300 text-xs font-medium text-stone-600 cursor-pointer min-h-[44px]">
                  <Camera className="w-4 h-4" /> Add Photo
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => handlePhotoUpload(it.id, e.target.files?.[0])}
                  />
                </label>
              </div>

              {/* Sub-Parts Checklist */}
              <div className="pt-2 border-t border-stone-100 space-y-2">
                <div className="text-xs font-bold uppercase text-stone-500">
                  Sub-Parts (Optional)
                </div>
                <div className="flex flex-wrap gap-2">
                  {(it.parts || []).map((pt) => (
                    <button
                      key={pt.id}
                      onClick={() => handleTogglePart(pt)}
                      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium border ${
                        pt.is_done
                          ? "bg-emerald-50 border-emerald-200 text-emerald-800 line-through"
                          : "bg-stone-50 border-stone-200 text-stone-800"
                      }`}
                    >
                      {pt.is_done ? (
                        <CheckSquare className="w-3.5 h-3.5" />
                      ) : (
                        <Square className="w-3.5 h-3.5" />
                      )}
                      {pt.part_name}
                    </button>
                  ))}
                </div>
                <div className="flex gap-2">
                  <input
                    placeholder="Add sub-part (e.g. Headboard, Shutters)"
                    value={newPartByItem[it.id] || ""}
                    onChange={(e) =>
                      setNewPartByItem({ ...newPartByItem, [it.id]: e.target.value })
                    }
                    className="flex-1 border border-stone-200 rounded-xl px-3 py-2 text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => handleAddPart(it.id)}
                    className="px-3 py-2 rounded-xl bg-stone-100 text-xs font-semibold"
                  >
                    + Part
                  </button>
                </div>
              </div>

              {/* Materials Checklist */}
              <div className="pt-2 border-t border-stone-100 space-y-2">
                <div className="text-xs font-bold uppercase text-stone-500">
                  Required Materials (Tap status to cycle)
                </div>
                <div className="space-y-1.5">
                  {(it.materials || []).map((m) => (
                    <div
                      key={m.id}
                      className="flex items-center justify-between text-sm bg-stone-50 rounded-xl px-3 py-2"
                    >
                      <span>
                        {m.material_name} —{" "}
                        <strong>
                          {m.quantity} {m.unit}
                        </strong>
                      </span>
                      <button
                        onClick={() => handleCycleMaterialStatus(m)}
                        className={`px-3 py-1 rounded-lg text-xs font-semibold ${
                          m.status === "needed"
                            ? "bg-rose-100 text-rose-800"
                            : m.status === "ordered"
                            ? "bg-amber-100 text-amber-900"
                            : "bg-emerald-100 text-emerald-800"
                        }`}
                      >
                        {m.status}
                      </button>
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
                  <select
                    value={matDraft.category || "ply_wood"}
                    onChange={(e) =>
                      setNewMatByItem({
                        ...newMatByItem,
                        [it.id]: { ...matDraft, category: e.target.value },
                      })
                    }
                    className="border border-stone-200 rounded-xl px-2 py-2 text-xs"
                  >
                    {categories.map((c) => (
                      <option key={c.code} value={c.code}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                  <input
                    placeholder="Material name"
                    value={matDraft.material_name || ""}
                    onChange={(e) =>
                      setNewMatByItem({
                        ...newMatByItem,
                        [it.id]: { ...matDraft, material_name: e.target.value },
                      })
                    }
                    className="col-span-2 border border-stone-200 rounded-xl px-3 py-2 text-xs"
                  />
                  <input
                    type="number"
                    placeholder="Qty"
                    value={matDraft.quantity || ""}
                    onChange={(e) =>
                      setNewMatByItem({
                        ...newMatByItem,
                        [it.id]: { ...matDraft, quantity: e.target.value },
                      })
                    }
                    className="border border-stone-200 rounded-xl px-2 py-2 text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => handleAddMaterial(it.id)}
                    className="px-3 py-2 rounded-xl bg-amber-800 text-white text-xs font-semibold"
                  >
                    + Material
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
