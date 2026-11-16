import { ArrowRight, Hammer, PackageCheck, UserCheck } from "lucide-react";
import { formatDate } from "../lib/format";

const NEXT_PIPELINE = {
  lead: { code: "design", label: "Move to Design" },
  design: { code: "estimating", label: "Send to Estimating" },
  estimating: { code: "quoted_awaiting_advance", label: "Mark Quoted" },
  quoted_awaiting_advance: { code: "site_sizing_boq", label: "Advance In → Sizing" },
  site_sizing_boq: { code: "execution", label: "BOQ Done → Execution" },
};

const NEXT_MATERIAL = {
  check_stock: { code: "ordered_pending", label: "Mark Ordered" },
  ordered_pending: { code: "all_in_shop", label: "All In Shop ✓" },
};

const NEXT_ITEM = {
  in_shop: { code: "packed_ready", label: "Packed / Ready" },
  packed_ready: { code: "at_site", label: "At Site" },
  at_site: { code: "qc_done", label: "QC Done ✓" },
};

function AgingBadge({ days }) {
  const d = Number(days || 0);
  const tone =
    d >= 5
      ? "bg-rose-100 text-rose-800 border-rose-200"
      : d >= 3
      ? "bg-amber-100 text-amber-900 border-amber-200"
      : "bg-stone-100 text-stone-700 border-stone-200";
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${tone}`}>
      {d === 0 ? "Today" : `${d}d`}
    </span>
  );
}

export default function WhosDoingWhat({
  me,
  people,
  whosDoingWhat,
  onQuickAdvance,
  onOpenOrder,
}) {
  const peopleById = Object.fromEntries((people || []).map((p) => [p.id, p]));
  const operators = (people || []).filter((p) => p.person_type === "operator");
  const mistris = (people || []).filter((p) => p.person_type !== "operator");

  const myPersonId = me?.person_id || null;
  const myItems = myPersonId
    ? (whosDoingWhat || []).filter((w) =>
        (w.assigned_person_ids || []).includes(myPersonId)
      )
    : [];

  const unassignedItems = (whosDoingWhat || []).filter(
    (w) => !(w.assigned_person_ids || []).length
  );

  function renderWorkRow(w) {
    let nextStep = null;
    if (w.work_kind === "project_pipeline") nextStep = NEXT_PIPELINE[w.stage_code];
    if (w.work_kind === "material_track") nextStep = NEXT_MATERIAL[w.stage_code];
    if (w.work_kind === "item_execution") nextStep = NEXT_ITEM[w.stage_code];

    return (
      <div
        key={`${w.work_kind}-${w.project_id}-${w.item_id || "p"}`}
        className="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 p-3 rounded-xl bg-stone-50 border border-stone-200/80"
      >
        <div
          onClick={() => onOpenOrder(w.project_id)}
          className="cursor-pointer space-y-0.5 min-w-0 flex-1"
        >
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-mono font-bold text-amber-900 bg-amber-100/80 px-2 py-0.5 rounded">
              #{w.project_code}
            </span>
            <span className="font-semibold text-sm text-stone-900 truncate">
              {w.customer_name}
            </span>
            <AgingBadge days={w.days_in_stage} />
          </div>
          <div className="text-xs text-stone-600 flex items-center gap-2 flex-wrap">
            <span className="font-medium text-stone-800">{w.stage_label}</span>
            {w.site_area_landmark && <span>• 📍 {w.site_area_landmark}</span>}
            {w.promised_delivery_date && (
              <span>• Due {formatDate(w.promised_delivery_date)}</span>
            )}
          </div>
        </div>

        {nextStep && (
          <button
            onClick={() => onQuickAdvance(w, nextStep.code)}
            className="min-h-[44px] px-3.5 py-2 rounded-xl bg-amber-900 hover:bg-amber-800 text-white text-xs font-semibold flex items-center justify-center gap-1.5 shrink-0 shadow-sm active:scale-[0.99]"
          >
            <span>{nextStep.label}</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6 pb-28">
      {/* 1. My Queue Strip */}
      <section className="bg-white rounded-2xl border border-amber-200 p-4 shadow-sm space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <UserCheck className="w-5 h-5 text-amber-800" />
            <h2 className="font-bold text-base text-stone-900">
              My Queue ({me?.display_name || "Operator"})
            </h2>
          </div>
          <span className="text-xs font-bold px-2.5 py-1 rounded-full bg-amber-100 text-amber-950">
            {myItems.length} active
          </span>
        </div>
        {myItems.length === 0 ? (
          <p className="text-sm text-stone-500 py-1">
            Nothing waiting directly on you right now.
          </p>
        ) : (
          <div className="space-y-2">{myItems.map(renderWorkRow)}</div>
        )}
      </section>

      {/* 2. Office Team Queues (Shreyansh, Ansh, Estimators) */}
      <section className="space-y-3">
        <div className="flex items-center gap-2 px-1">
          <PackageCheck className="w-4 h-4 text-stone-700" />
          <h3 className="text-xs font-bold uppercase tracking-wider text-stone-600">
            Office Team Queues (Logged-In Operators)
          </h3>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {operators.map((op) => {
            const items = (whosDoingWhat || []).filter((w) =>
              (w.assigned_person_ids || []).includes(op.id)
            );
            return (
              <div
                key={op.id}
                className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm"
              >
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-bold text-stone-900 text-sm">
                      {op.full_name}
                    </div>
                    <div className="text-xs text-stone-500">
                      {(op.role_tags || []).join(" · ") || "Operator"}
                    </div>
                  </div>
                  <span className="text-xs font-bold px-2.5 py-1 rounded-full bg-stone-100 text-stone-800">
                    {items.length}
                  </span>
                </div>
                {items.length === 0 ? (
                  <p className="text-xs text-stone-400">Clear queue</p>
                ) : (
                  <div className="space-y-2">{items.map(renderWorkRow)}</div>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* 3. Mistri Floor & Site Load (Managed by Shreyansh / Office) */}
      <section className="space-y-3">
        <div className="flex items-center justify-between px-1">
          <div className="flex items-center gap-2">
            <Hammer className="w-4 h-4 text-stone-700" />
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-600">
              Mistri Floor &amp; Site Load (Offline Assignees)
            </h3>
          </div>
          <span className="text-xs text-stone-500">
            {mistris.length} Mistris in roster
          </span>
        </div>

        {mistris.length === 0 ? (
          <div className="bg-white rounded-2xl border border-dashed border-stone-300 p-5 text-center text-sm text-stone-500">
            No Mistris added yet — tap <b>Roster</b> in the top bar (or tell the AI bar{" "}
            <i>&ldquo;Add Ramesh Mistri to roster&rdquo;</i>) to track shop &amp; site load.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {mistris.map((m) => {
              const items = (whosDoingWhat || []).filter((w) =>
                (w.assigned_person_ids || []).includes(m.id)
              );
              return (
                <div
                  key={m.id}
                  className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3 shadow-sm"
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="font-bold text-stone-900 text-sm">
                        {m.full_name}
                      </div>
                      <div className="text-xs text-stone-500">
                        {items.length === 0
                          ? "Available for assignment"
                          : `${items.length} active piece(s)/sizing`}
                      </div>
                    </div>
                    <span
                      className={`text-xs font-bold px-2.5 py-1 rounded-full ${
                        items.length > 0
                          ? "bg-amber-100 text-amber-950"
                          : "bg-emerald-50 text-emerald-800"
                      }`}
                    >
                      {items.length === 0 ? "Free" : `${items.length} Active`}
                    </span>
                  </div>
                  {items.length > 0 && (
                    <div className="space-y-2">{items.map(renderWorkRow)}</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* 4. Unassigned Action Items */}
      {unassignedItems.length > 0 && (
        <section className="bg-white rounded-2xl border border-stone-200 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="font-bold text-sm text-stone-800">
              Unassigned / Waiting Assignment ({unassignedItems.length})
            </h3>
          </div>
          <div className="space-y-2">
            {unassignedItems.map((w) => (
              <div key={`${w.work_kind}-${w.project_id}-${w.item_id || "u"}`}>
                {renderWorkRow(w)}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
