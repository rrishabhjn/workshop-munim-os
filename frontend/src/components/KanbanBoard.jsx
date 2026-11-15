import { useState } from "react";
import { ArrowRight, Settings, ArrowUp, ArrowDown, Plus } from "lucide-react";
import { apiFetch } from "../lib/api";

export default function KanbanBoard({ projects, stages, onSelectProject, onRefresh }) {
  const [warning, setWarning] = useState("");
  const [dragItemId, setDragItemId] = useState(null);
  const [showManage, setShowManage] = useState(false);
  const [newStageLabel, setNewStageLabel] = useState("");

  // Flatten active project items with customer context
  const allItems = projects
    .filter((p) => p.status === "active")
    .flatMap((p) =>
      (p.items || []).map((it) => ({
        ...it,
        customer_name: p.customer_name,
        project_code: p.project_code,
      }))
    );

  async function moveStage(itemId, newStageCode) {
    const res = await apiFetch(`/api/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify({ stage: newStageCode }),
    });
    if (res.needed_materials_warning_count > 0) {
      setWarning(
        `Soft Warning: Moved item still has ${res.needed_materials_warning_count} material(s) marked "Needed".`
      );
    } else {
      setWarning("");
    }
    onRefresh();
  }

  async function handleAddStage(e) {
    e.preventDefault();
    if (!newStageLabel.trim()) return;
    await apiFetch("/api/stages", {
      method: "POST",
      body: JSON.stringify({ label: newStageLabel.trim() }),
    });
    setNewStageLabel("");
    onRefresh();
  }

  async function handleRenameStage(code, currentLabel) {
    const next = window.prompt("Rename stage:", currentLabel);
    if (!next || next.trim() === currentLabel) return;
    await apiFetch(`/api/stages/${code}`, {
      method: "PATCH",
      body: JSON.stringify({ label: next.trim() }),
    });
    onRefresh();
  }

  async function handleShiftStage(index, dir) {
    const nextIdx = index + dir;
    if (nextIdx < 0 || nextIdx >= stages.length) return;
    const codes = stages.map((s) => s.code);
    [codes[index], codes[nextIdx]] = [codes[nextIdx], codes[index]];
    await apiFetch("/api/stages/reorder", {
      method: "POST",
      body: JSON.stringify({ codes }),
    });
    onRefresh();
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-stone-500">
          Tap Next / dropdown on mobile, or drag cards on desktop.
        </p>
        <button
          onClick={() => setShowManage(true)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-white border border-stone-200 text-sm font-semibold"
        >
          <Settings className="w-4 h-4" /> Manage Stages
        </button>
      </div>

      {warning && (
        <div className="bg-amber-50 border border-amber-300 text-amber-900 px-4 py-3 rounded-xl text-sm font-medium">
          {warning}
        </div>
      )}

      <div className="flex gap-3 overflow-x-auto pb-4">
        {stages.map((st, idx) => {
          const colItems = allItems.filter((it) => it.stage === st.code);
          const nextStage = stages[idx + 1];

          return (
            <div
              key={st.code}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => {
                if (dragItemId) {
                  moveStage(dragItemId, st.code);
                  setDragItemId(null);
                }
              }}
              className="min-w-[270px] w-[270px] flex-shrink-0 bg-stone-100 rounded-2xl p-3 border border-stone-200 flex flex-col gap-2.5"
            >
              <div className="flex items-center justify-between px-1">
                <h3 className="font-bold text-sm text-stone-800">{st.label}</h3>
                <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-white text-stone-700">
                  {colItems.length}
                </span>
              </div>

              <div className="space-y-2.5">
                {colItems.map((it) => (
                  <div
                    key={it.id}
                    draggable
                    onDragStart={() => setDragItemId(it.id)}
                    className="bg-white rounded-xl p-3 border border-stone-200 shadow-sm space-y-2 cursor-grab active:cursor-grabbing"
                  >
                    <button
                      onClick={() => onSelectProject(it.project_id)}
                      className="text-left w-full"
                    >
                      <div className="text-xs font-medium text-amber-800">
                        {it.customer_name} • {it.project_code}
                      </div>
                      <div className="font-bold text-stone-900">{it.item_name}</div>
                      {it.assigned_karigar && (
                        <div className="text-xs text-stone-500">
                          Karigar: {it.assigned_karigar}
                        </div>
                      )}
                    </button>

                    {(it.parts || []).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {it.parts.map((pt) => (
                          <span
                            key={pt.id}
                            className={`text-[11px] px-2 py-0.5 rounded-md border ${
                              pt.is_done
                                ? "bg-emerald-50 border-emerald-200 text-emerald-800 line-through"
                                : "bg-stone-50 border-stone-200 text-stone-700"
                            }`}
                          >
                            {pt.part_name}
                          </span>
                        ))}
                      </div>
                    )}

                    <div className="flex items-center gap-1.5 pt-1">
                      <select
                        value={it.stage}
                        onChange={(e) => moveStage(it.id, e.target.value)}
                        className="flex-1 text-xs border border-stone-200 rounded-lg px-2 py-1.5 bg-stone-50"
                      >
                        {stages.map((s) => (
                          <option key={s.code} value={s.code}>
                            {s.label}
                          </option>
                        ))}
                      </select>

                      {nextStage && (
                        <button
                          onClick={() => moveStage(it.id, nextStage.code)}
                          title={`Move to ${nextStage.label}`}
                          className="px-2.5 py-1.5 rounded-lg bg-amber-800 text-white text-xs font-semibold inline-flex items-center gap-1"
                        >
                          Next <ArrowRight className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {showManage && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-end sm:items-center justify-center p-4">
          <div className="bg-white w-full max-w-md rounded-2xl p-5 space-y-4 shadow-xl max-h-[85vh] overflow-y-auto">
            <div className="flex justify-between items-center">
              <h3 className="text-lg font-bold">Manage Kanban Stages</h3>
              <button
                onClick={() => setShowManage(false)}
                className="text-sm font-semibold text-stone-500 px-3 py-1"
              >
                Close
              </button>
            </div>

            <div className="space-y-2">
              {stages.map((st, idx) => (
                <div
                  key={st.code}
                  className="flex items-center justify-between gap-2 bg-stone-50 border border-stone-200 rounded-xl px-3 py-2"
                >
                  <button
                    onClick={() => handleRenameStage(st.code, st.label)}
                    className="text-left flex-1 font-medium text-sm"
                  >
                    {st.label}{" "}
                    {st.is_terminal && (
                      <span className="text-[11px] text-emerald-700 font-bold">
                        (Terminal)
                      </span>
                    )}
                  </button>
                  <div className="flex gap-1">
                    <button
                      onClick={() => handleShiftStage(idx, -1)}
                      disabled={idx === 0}
                      className="p-2 rounded-lg bg-white border border-stone-200 disabled:opacity-40"
                    >
                      <ArrowUp className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => handleShiftStage(idx, 1)}
                      disabled={idx === stages.length - 1}
                      className="p-2 rounded-lg bg-white border border-stone-200 disabled:opacity-40"
                    >
                      <ArrowDown className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </div>

            <form onSubmit={handleAddStage} className="flex gap-2 pt-2 border-t border-stone-100">
              <input
                placeholder="New stage (e.g. CNC / Carving)"
                value={newStageLabel}
                onChange={(e) => setNewStageLabel(e.target.value)}
                className="flex-1 border border-stone-300 rounded-xl px-3 py-2 text-sm"
              />
              <button
                type="submit"
                className="px-4 py-2 rounded-xl bg-amber-800 text-white text-sm font-semibold inline-flex items-center gap-1"
              >
                <Plus className="w-4 h-4" /> Add
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
