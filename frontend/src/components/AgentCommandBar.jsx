import { useEffect, useRef, useState } from "react";
import {
  Check,
  ExternalLink,
  Inbox,
  Loader2,
  Paperclip,
  Pencil,
  Send,
  Undo2,
  X,
} from "lucide-react";
import { apiFetch } from "../lib/api";

export default function AgentCommandBar({
  domainScope,
  viewingProjectId,
  paymentAccounts = [],
  needsInfo,
  showNeedsInfo,
  onCloseNeedsInfo,
  undoToast,
  onDismissUndoToast,
  onTriggerUndoEvents,
  onAfterAgentMutation,
  onViewOrder,
}) {
  const [inputText, setInputText] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingStatus, setPendingStatus] = useState("");
  const [attachedFile, setAttachedFile] = useState(null);
  const [lastTurn, setLastTurn] = useState(null);
  const [qaThread, setQaThread] = useState([]);
  const [sheetHidden, setSheetHidden] = useState(false);
  const [aiToast, setAiToast] = useState(null);
  const [editingGoalId, setEditingGoalId] = useState(null);
  const [editDraft, setEditDraft] = useState({});
  const [editNote, setEditNote] = useState("");
  const [errorMsg, setErrorMsg] = useState("");

  const WINDOW_MS = 30 * 60 * 1000;
  const fileInputRef = useRef(null);
  const toastTimerRef = useRef(null);
  const sheetScrollRef = useRef(null);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!sheetHidden && sheetScrollRef.current) {
      sheetScrollRef.current.scrollTop = sheetScrollRef.current.scrollHeight;
    }
  }, [qaThread.length, lastTurn, sheetHidden]);

  function showAiToast(payload, durationMs = 6000) {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setAiToast(payload);
    toastTimerRef.current = setTimeout(() => {
      setAiToast(null);
    }, durationMs);
  }

  function dismissAiToast() {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setAiToast(null);
  }

  async function submitTurn({ textOverride, audioBlob }) {
    const textVal = (textOverride ?? inputText).trim();
    if (!textVal && !audioBlob && !attachedFile) return;

    const submittedText = textOverride === undefined ? inputText : textVal;
    const submittedFile = attachedFile;

    // Clear input optimistically and show progress pill immediately
    if (textOverride === undefined) setInputText("");
    setAttachedFile(null);
    setErrorMsg("");
    setSending(true);
    setPendingStatus(
      audioBlob
        ? "🎙️ Listening & updating…"
        : `⚡ Updating: "${
            textVal.length > 52 ? `${textVal.slice(0, 52)}…` : textVal || "Attachment"
          }"`
    );

    try {
      const fd = new FormData();
      fd.append("text", textVal);
      fd.append("domain_scope", domainScope);
      fd.append(
        "ui_context_json",
        JSON.stringify({
          viewing_project_id: viewingProjectId || null,
          domain_scope: domainScope,
        })
      );
      if (audioBlob) {
        fd.append("audio", audioBlob, "voice.webm");
      }
      if (submittedFile) {
        fd.append("attachment", submittedFile);
      }

      const res = await apiFetch("/api/agent/turn", {
        method: "POST",
        body: fd,
      });

      if (res?.is_error) {
        if (submittedText) setInputText((cur) => cur || submittedText);
        if (submittedFile) setAttachedFile((cur) => cur || submittedFile);
        setLastTurn(null);
        setErrorMsg(res.reply_text || "Could not process command.");
        return;
      }

      const goals = res?.goals || [];
      const allAutoCompleted =
        goals.length > 0 &&
        goals.every((g) => g.status === "completed") &&
        !(res?.reply_text || "").trim();

      if (allAutoCompleted) {
        setLastTurn(null);
        const receiptMsg = goals
          .map((g) => g.receipt || g.summary_title)
          .filter(Boolean)
          .join(" · ");
        const firstProjectGoal = goals.find((g) => g.project_id);
        const undoableGoal = goals.find((g) => g.undo_available && g.goal_id);
        showAiToast(
          {
            message: receiptMsg || "Updated",
            projectId: firstProjectGoal?.project_id || null,
            goalId: undoableGoal?.goal_id || null,
            undoAvailable: Boolean(undoableGoal),
            heardTranscript: res?.heard_transcript || null,
          },
          6000
        );
        Promise.resolve(onAfterAgentMutation()).catch(() => {});
      } else {
        setSheetHidden(false);
        const replyTxt = (res?.reply_text || "").trim();
        if (replyTxt) {
          const nowTs = Date.now();
          setQaThread((prev) => {
            const freshPrev =
              prev.length > 0 && nowTs - prev[prev.length - 1].ts <= WINDOW_MS
                ? prev
                : [];
            return [
              ...freshPrev,
              {
                id: String(nowTs),
                userText:
                  textVal || (audioBlob ? "🎙️ Voice question" : "Attachment"),
                heardTranscript: res?.heard_transcript || null,
                replyText: replyTxt,
                ts: nowTs,
              },
            ].slice(-10);
          });
          setLastTurn(goals.length > 0 ? { ...res, reply_text: null } : null);
        } else {
          setLastTurn(res);
        }
        await onAfterAgentMutation();
      }
    } catch (err) {
      if (submittedText) setInputText((cur) => cur || submittedText);
      if (submittedFile) setAttachedFile((cur) => cur || submittedFile);
      setErrorMsg(err.message || "AI request failed");
    } finally {
      setSending(false);
      setPendingStatus("");
    }
  }

  async function handleGoalAction(goalId, action, extraBody = null, goalObj = null) {
    setSending(true);
    setErrorMsg("");
    try {
      const res = await apiFetch(`/api/agent/goals/${goalId}/${action}`, {
        method: "POST",
        body: extraBody ? JSON.stringify(extraBody) : undefined,
      });

      if (action === "undo" && res?.ok) {
        showAiToast({ message: "↩️ Action undone", undoAvailable: false }, 3500);
        if (lastTurn?.goals) {
          setLastTurn((prev) =>
            prev
              ? {
                  ...prev,
                  goals: prev.goals.filter((g) => g.goal_id !== goalId),
                }
              : null
          );
        }
        Promise.resolve(onAfterAgentMutation()).catch(() => {});
        return;
      }

      if (action === "discard" && res?.ok) {
        if (lastTurn?.goals) {
          setLastTurn((prev) => {
            if (!prev) return null;
            const remaining = prev.goals.filter((g) => g.goal_id !== goalId);
            if (remaining.length === 0 && !(prev.reply_text || "").trim()) {
              return null;
            }
            return { ...prev, goals: remaining };
          });
        }
        await onAfterAgentMutation();
        return;
      }

      if (res?.status === "completed") {
        if (lastTurn?.goals) {
          setLastTurn((prev) => {
            if (!prev) return null;
            const remaining = prev.goals.filter((g) => g.goal_id !== goalId);
            if (remaining.length === 0 && !(prev.reply_text || "").trim()) {
              return null;
            }
            return { ...prev, goals: remaining };
          });
        }
        showAiToast(
          {
            message: res.receipt || goalObj?.summary_title || "Saved",
            projectId: res.project_id || goalObj?.project_id || null,
            goalId,
            undoAvailable: Boolean(res.undo_available),
          },
          6000
        );
        Promise.resolve(onAfterAgentMutation()).catch(() => {});
        return;
      }

      if (lastTurn?.goals) {
        setLastTurn((prev) =>
          prev
            ? {
                ...prev,
                goals: prev.goals.map((g) =>
                  g.goal_id === goalId ? { ...g, ...res } : g
                ),
              }
            : null
        );
      }
      await onAfterAgentMutation();
    } catch (err) {
      setErrorMsg(err.message || "Action failed");
    } finally {
      setSending(false);
    }
  }

  function openInlineEdit(g) {
    const slots = g.collected_slots || {};
    setEditingGoalId(g.goal_id);
    setEditDraft({
      party_name: slots.party_name ?? "",
      amount_inr: slots.amount_inr ?? "",
      payment_account_code: slots.payment_account_code ?? "",
      full_name: slots.full_name ?? "",
      area_landmark: slots.area_landmark ?? "",
      rough_scope: slots.rough_scope ?? "",
      phone: slots.phone ?? "",
      quoted_total: slots.quoted_total ?? "",
      notes: slots.notes ?? "",
    });
    setEditNote("");
  }

  async function handleSaveInlineEdit(e, g) {
    e.preventDefault();
    const gid = g.goal_id;
    const orig = g.collected_slots || {};
    const patch = {};

    const textKeys = [
      "party_name",
      "payment_account_code",
      "full_name",
      "area_landmark",
      "rough_scope",
      "phone",
      "notes",
    ];
    for (const k of textKeys) {
      const val = String(editDraft[k] ?? "").trim();
      const origVal = String(orig[k] ?? "").trim();
      if (val && val !== origVal) {
        patch[k] = val;
      }
    }
    if (patch.payment_account_code && paymentAccounts.length > 0) {
      const acc = paymentAccounts.find((a) => a.code === patch.payment_account_code);
      if (acc?.id) patch.payment_account_id = acc.id;
    }
    if (editDraft.amount_inr !== "" && editDraft.amount_inr !== undefined) {
      const num = Number(editDraft.amount_inr);
      if (!Number.isNaN(num) && num !== Number(orig.amount_inr ?? NaN)) {
        patch.amount_inr = num;
      }
    }
    if (editDraft.quoted_total !== "" && editDraft.quoted_total !== undefined) {
      const qNum = Number(editDraft.quoted_total);
      if (!Number.isNaN(qNum) && qNum !== Number(orig.quoted_total ?? NaN)) {
        patch.quoted_total = qNum;
      }
    }

    setSending(true);
    setErrorMsg("");
    try {
      const res = await apiFetch(`/api/agent/goals/${gid}/edit`, {
        method: "POST",
        body: JSON.stringify({
          slot_patch: patch,
          correction_note: editNote.trim() || null,
        }),
      });
      setEditingGoalId(null);
      if (lastTurn?.goals) {
        setLastTurn((prev) =>
          prev
            ? {
                ...prev,
                goals: prev.goals.map((item) =>
                  item.goal_id === gid ? { ...item, ...res } : item
                ),
              }
            : null
        );
      }
      await onAfterAgentMutation();
    } catch (err) {
      setErrorMsg(err.message || "Edit failed");
    } finally {
      setSending(false);
    }
  }

  function renderGoalCard(g) {
    const gid = g.goal_id;
    const choices = g.choices || g.repair_options || [];
    const slots = g.collected_slots || {};
    const isEditing = editingGoalId === gid;
    const isFinancial =
      slots.amount_inr !== undefined ||
      Boolean(slots.payment_account_code) ||
      [
        "customer_receipt",
        "vendor_payment",
        "expense",
        "float_transfer",
        "supplier_draft_bill",
      ].includes(slots.operation_kind || g.goal_type);
    const isLeadOrEntity =
      slots.full_name !== undefined ||
      slots.area_landmark !== undefined ||
      slots.rough_scope !== undefined ||
      slots.quoted_total !== undefined;

    return (
      <div
        key={gid}
        className="p-3 rounded-xl bg-stone-50 border border-stone-200 space-y-2 text-xs"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-bold text-stone-900">
            {g.summary_title || g.goal_type || "Action"}
          </span>
          <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-950 font-semibold">
            {g.status}
          </span>
        </div>

        {(g.prompt || g.pending_prompt || g.receipt) && (
          <div className="text-stone-700 whitespace-pre-line">
            {g.receipt || g.prompt || g.pending_prompt}
          </div>
        )}

        {isEditing && (
          <form
            onSubmit={(e) => handleSaveInlineEdit(e, g)}
            className="bg-white border border-stone-300 rounded-xl p-2.5 space-y-2"
          >
            {isFinancial && (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <input
                  value={editDraft.party_name}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, party_name: e.target.value }))
                  }
                  placeholder="Party / Vendor Name"
                  className="min-h-[38px] px-2.5 rounded-lg border border-stone-300 text-xs"
                />
                <input
                  type="number"
                  value={editDraft.amount_inr}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, amount_inr: e.target.value }))
                  }
                  placeholder="Amount ₹"
                  className="min-h-[38px] px-2.5 rounded-lg border border-stone-300 text-xs"
                />
                <select
                  value={editDraft.payment_account_code}
                  onChange={(e) =>
                    setEditDraft((d) => ({
                      ...d,
                      payment_account_code: e.target.value,
                    }))
                  }
                  className="min-h-[38px] px-2 rounded-lg border border-stone-300 text-xs bg-white"
                >
                  <option value="">Select Account...</option>
                  {paymentAccounts.map((a) => (
                    <option key={a.code} value={a.code}>
                      {a.display_name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {isLeadOrEntity && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <input
                  value={editDraft.full_name}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, full_name: e.target.value }))
                  }
                  placeholder="Customer / Name"
                  className="min-h-[38px] px-2.5 rounded-lg border border-stone-300 text-xs"
                />
                <input
                  value={editDraft.area_landmark}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, area_landmark: e.target.value }))
                  }
                  placeholder="Site Area / City"
                  className="min-h-[38px] px-2.5 rounded-lg border border-stone-300 text-xs"
                />
                <input
                  value={editDraft.rough_scope}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, rough_scope: e.target.value }))
                  }
                  placeholder="Rough Scope"
                  className="min-h-[38px] px-2.5 rounded-lg border border-stone-300 text-xs"
                />
                <input
                  value={editDraft.phone}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, phone: e.target.value }))
                  }
                  placeholder="Phone (optional)"
                  className="min-h-[38px] px-2.5 rounded-lg border border-stone-300 text-xs"
                />
              </div>
            )}

            <input
              value={editNote}
              onChange={(e) => setEditNote(e.target.value)}
              placeholder="Optional rule/note (e.g. Lala Ji transfers always go to Ruksar Bano)"
              className="w-full min-h-[36px] px-2.5 rounded-lg border border-stone-200 text-xs"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setEditingGoalId(null)}
                className="min-h-[36px] px-3 rounded-lg border border-stone-300 text-stone-700 font-semibold"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={sending}
                className="min-h-[36px] px-3.5 rounded-lg bg-amber-900 text-white font-bold"
              >
                Save Edit
              </button>
            </div>
          </form>
        )}

        <div className="flex items-center gap-2 flex-wrap pt-1">
          {g.status === "completed" && g.undo_available && (
            <button
              onClick={() => handleGoalAction(gid, "undo", null, g)}
              className="min-h-[38px] px-3 rounded-lg border border-stone-300 bg-white font-semibold text-stone-800 flex items-center gap-1"
            >
              <Undo2 className="w-3.5 h-3.5" />
              <span>Undo</span>
            </button>
          )}

          {g.status === "awaiting_confirmation" && (
            <>
              <button
                onClick={() => handleGoalAction(gid, "confirm", null, g)}
                className="min-h-[40px] px-3.5 rounded-xl bg-emerald-700 text-white font-bold flex items-center gap-1"
              >
                <Check className="w-3.5 h-3.5" />
                <span>Confirm</span>
              </button>
              <button
                onClick={() => openInlineEdit(g)}
                className="min-h-[40px] px-3 rounded-xl border border-stone-300 bg-white font-semibold text-stone-800 flex items-center gap-1"
              >
                <Pencil className="w-3.5 h-3.5" />
                <span>Edit</span>
              </button>
              <button
                onClick={() => handleGoalAction(gid, "discard", null, g)}
                className="min-h-[40px] px-3 rounded-xl border border-rose-200 bg-rose-50 font-semibold text-rose-700"
              >
                Discard
              </button>
            </>
          )}

          {(g.status === "gathering_info" ||
            g.status === "needs_error_fix" ||
            g.status === "parked_for_clerk") && (
            <>
              {choices.map((ch, i) => {
                let parsedPatch = {};
                try {
                  parsedPatch =
                    typeof ch.slot_patch_json === "string"
                      ? JSON.parse(ch.slot_patch_json)
                      : ch.slot_patch || {};
                } catch {
                  parsedPatch = {};
                }
                return (
                  <button
                    key={ch.choice_id || i}
                    onClick={() =>
                      handleGoalAction(
                        gid,
                        "choice",
                        { slot_patch: parsedPatch },
                        g
                      )
                    }
                    className="min-h-[40px] px-3 rounded-xl bg-amber-900 text-white font-semibold"
                  >
                    {ch.label}
                  </button>
                );
              })}

              {g.status === "needs_error_fix" && (
                <button
                  onClick={() => handleGoalAction(gid, "park", null, g)}
                  className="min-h-[40px] px-3 rounded-xl border border-stone-300 bg-white font-semibold text-stone-800"
                >
                  📥 Let Clerk Fix Later
                </button>
              )}

              {g.status === "parked_for_clerk" && (
                <button
                  onClick={() => handleGoalAction(gid, "confirm", null, g)}
                  className="min-h-[40px] px-3 rounded-xl bg-emerald-700 text-white font-bold"
                >
                  Retry Zoho Push
                </button>
              )}

              <button
                onClick={() => handleGoalAction(gid, "discard", null, g)}
                className="min-h-[40px] px-3 rounded-xl border border-rose-200 bg-rose-50 font-semibold text-rose-700"
              >
                Discard
              </button>
            </>
          )}
        </div>
      </div>
    );
  }

  const nowMs = Date.now();
  const recentThread =
    qaThread.length > 0 && nowMs - qaThread[qaThread.length - 1].ts <= WINDOW_MS
      ? qaThread
      : [];
  const hasPendingGoals = Boolean(lastTurn?.goals && lastTurn.goals.length > 0);
  const showSheet =
    Boolean(errorMsg) ||
    (!sheetHidden && (recentThread.length > 0 || hasPendingGoals));

  return (
    <>
      {/* Needs Info / Parked Clerk Drawer */}
      {showNeedsInfo && (
        <div className="fixed inset-0 z-50 bg-black/40 flex justify-end">
          <div className="bg-white w-full max-w-md h-full flex flex-col shadow-2xl">
            <div className="p-4 border-b border-stone-200 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Inbox className="w-5 h-5 text-amber-900" />
                <div>
                  <h2 className="font-bold text-base text-stone-900">
                    Needs Info &amp; Clerk Queue ({(needsInfo || []).length})
                  </h2>
                  <p className="text-xs text-stone-500">
                    Open clarifications &amp; parked Zoho validation fixes (never auto-expires)
                  </p>
                </div>
              </div>
              <button
                onClick={onCloseNeedsInfo}
                className="min-h-[44px] min-w-[44px] flex items-center justify-center rounded-xl hover:bg-stone-100"
              >
                <X className="w-5 h-5 text-stone-600" />
              </button>
            </div>

            <div className="p-4 overflow-y-auto flex-1 space-y-3">
              {(needsInfo || []).length === 0 ? (
                <p className="text-sm text-stone-500 text-center py-8">
                  All clear — zero unfinished clarifications or Zoho errors!
                </p>
              ) : (
                (needsInfo || []).map((n) => renderGoalCard(n))
              )}
            </div>
          </div>
        </div>
      )}

      {/* Persistent Bottom Command Bar + Floating Response / Undo Sheet */}
      <div className="fixed bottom-0 inset-x-0 z-[45] pointer-events-none">
        <div className="max-w-4xl mx-auto px-3 pb-3 space-y-2 pointer-events-auto">
          {/* Quick UI Stage Undo Toast */}
          {undoToast && (
            <div className="bg-stone-900 text-white rounded-2xl px-4 py-2.5 shadow-lg flex items-center justify-between gap-3 text-xs">
              <span className="truncate">{undoToast.message}</span>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={() => onTriggerUndoEvents(undoToast.eventIds)}
                  className="min-h-[36px] px-3 rounded-xl bg-amber-400 text-stone-950 font-bold flex items-center gap-1"
                >
                  <Undo2 className="w-3.5 h-3.5" />
                  <span>Undo</span>
                </button>
                <button onClick={onDismissUndoToast} className="p-1">
                  <X className="w-4 h-4 text-stone-400" />
                </button>
              </div>
            </div>
          )}

          {/* Optimistic Progress Pill While AI Turn Runs */}
          {pendingStatus && (
            <div className="bg-amber-50 border border-amber-300 text-amber-950 rounded-2xl px-3.5 py-2.5 shadow-md flex items-center gap-2.5 text-xs font-semibold">
              <Loader2 className="w-4 h-4 animate-spin text-amber-800 shrink-0" />
              <span className="truncate">{pendingStatus}</span>
            </div>
          )}

          {/* Auto-Commit 6s Toast (with View Order + Undo) */}
          {aiToast && (
            <div className="bg-stone-900 text-white rounded-2xl px-4 py-3 shadow-xl flex items-center justify-between gap-2.5 text-xs">
              <div className="min-w-0 flex-1 space-y-0.5">
                {aiToast.heardTranscript && (
                  <div className="text-amber-300 italic truncate">
                    🎙️ &ldquo;{aiToast.heardTranscript}&rdquo;
                  </div>
                )}
                <div className="font-medium break-words">
                  {aiToast.message}
                </div>
              </div>
              <div className="flex items-center gap-1.5 shrink-0">
                {aiToast.projectId && onViewOrder && (
                  <button
                    onClick={() => {
                      const pid = aiToast.projectId;
                      dismissAiToast();
                      onViewOrder(pid);
                    }}
                    className="min-h-[36px] px-2.5 rounded-xl text-amber-300 hover:bg-stone-800 font-bold flex items-center gap-1 underline"
                  >
                    <span>View Order</span>
                    <ExternalLink className="w-3 h-3" />
                  </button>
                )}
                {aiToast.undoAvailable && aiToast.goalId && (
                  <button
                    onClick={() =>
                      handleGoalAction(aiToast.goalId, "undo", null, null)
                    }
                    className="min-h-[36px] px-3 rounded-xl bg-amber-400 text-stone-950 font-bold flex items-center gap-1"
                  >
                    <Undo2 className="w-3.5 h-3.5" />
                    <span>Undo</span>
                  </button>
                )}
                <button
                  onClick={dismissAiToast}
                  className="p-1 rounded-lg hover:bg-stone-800"
                >
                  <X className="w-4 h-4 text-stone-400" />
                </button>
              </div>
            </div>
          )}

          {/* Reopen Recent Q&A Chip When Sheet Is Hidden */}
          {sheetHidden &&
            recentThread.length > 0 &&
            !pendingStatus &&
            !aiToast && (
              <div className="flex justify-start">
                <button
                  type="button"
                  onClick={() => setSheetHidden(false)}
                  className="px-3.5 py-1.5 rounded-full bg-white border border-stone-300 shadow-sm text-xs font-semibold text-stone-700 hover:bg-stone-50"
                >
                  💬 Show recent Q&amp;A ({recentThread.length})
                </button>
              </div>
            )}

          {/* Latest AI Thread & Confirmation Sheet */}
          {showSheet && (
            <div
              ref={sheetScrollRef}
              className="bg-white rounded-2xl border-2 border-amber-300 p-3.5 shadow-xl space-y-2.5 max-h-80 overflow-y-auto"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-bold uppercase tracking-wide text-stone-500">
                  {recentThread.length > 0
                    ? `💬 Assistant Thread (${recentThread.length})`
                    : "⚡ Action Review"}
                </span>
                <button
                  onClick={() => {
                    setSheetHidden(true);
                    setErrorMsg("");
                  }}
                  title="Hide sheet (keeps 30m conversation context)"
                  className="p-1 rounded-lg hover:bg-stone-100"
                >
                  <X className="w-4 h-4 text-stone-500" />
                </button>
              </div>

              {errorMsg && (
                <div className="text-xs text-rose-700 font-semibold bg-rose-50 border border-rose-200 rounded-xl px-3 py-2">
                  {errorMsg}
                </div>
              )}

              {recentThread.length > 0 && (
                <div className="space-y-2.5">
                  {recentThread.map((item) => (
                    <div key={item.id} className="space-y-1">
                      <div className="flex justify-end">
                        <div className="bg-amber-900 text-white rounded-xl rounded-br-sm px-3 py-1.5 text-xs max-w-[85%] space-y-0.5">
                          <div>{item.userText}</div>
                          {item.heardTranscript && (
                            <div className="text-amber-200 italic text-[11px]">
                              🎙️ &ldquo;{item.heardTranscript}&rdquo;
                            </div>
                          )}
                        </div>
                      </div>
                      <div className="bg-stone-50 border border-stone-200 rounded-xl rounded-bl-sm px-3 py-2 text-xs sm:text-sm text-stone-800 whitespace-pre-line">
                        {item.replyText}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {hasPendingGoals && (
                <div className="space-y-2">
                  {lastTurn.goals.map((g) => renderGoalCard(g))}
                </div>
              )}
            </div>
          )}

          {/* Input Pill */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              submitTurn({});
            }}
            className="bg-white rounded-2xl border border-stone-300 shadow-lg p-1.5 flex items-center gap-1.5"
          >
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*,application/pdf"
              onChange={(e) => setAttachedFile(e.target.files?.[0] || null)}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              title="Attach bill PDF or photo"
              className={`min-h-[44px] min-w-[44px] rounded-xl flex items-center justify-center ${
                attachedFile
                  ? "bg-amber-100 text-amber-900"
                  : "text-stone-500 hover:bg-stone-100"
              }`}
            >
              <Paperclip className="w-5 h-5" />
            </button>

            <input
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              placeholder={
                domainScope === "munim"
                  ? "Type money/bill update or question (Hinglish)..."
                  : viewingProjectId
                  ? "Update this open order, assign Mistri, or ask AI..."
                  : "Type lead, stage move, Mistri assign, or question..."
              }
              className="flex-1 min-h-[44px] px-2 text-sm outline-none bg-transparent text-stone-900"
            />

            <button
              type="submit"
              disabled={sending || (!inputText.trim() && !attachedFile)}
              className="min-h-[44px] px-4 rounded-xl bg-amber-900 text-white font-bold text-xs flex items-center gap-1.5 disabled:opacity-40"
            >
              <Send className="w-4 h-4" />
              <span className="hidden sm:inline">
                {sending ? "..." : "Send"}
              </span>
            </button>
          </form>
        </div>
      </div>
    </>
  );
}
