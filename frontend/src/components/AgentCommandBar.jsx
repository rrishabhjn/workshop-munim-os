import { useRef, useState } from "react";
import {
  Check,
  Inbox,
  Mic,
  Paperclip,
  Send,
  Square,
  Undo2,
  X,
} from "lucide-react";
import { apiFetch } from "../lib/api";

export default function AgentCommandBar({
  domainScope,
  viewingProjectId,
  needsInfo,
  showNeedsInfo,
  onCloseNeedsInfo,
  undoToast,
  onDismissUndoToast,
  onTriggerUndoEvents,
  onAfterAgentMutation,
}) {
  const [inputText, setInputText] = useState("");
  const [sending, setSending] = useState(false);
  const [recording, setRecording] = useState(false);
  const [attachedFile, setAttachedFile] = useState(null);
  const [lastTurn, setLastTurn] = useState(null);
  const [errorMsg, setErrorMsg] = useState("");

  const mediaRecorderRef = useRef(null);
  const chunksRef = useRef([]);
  const fileInputRef = useRef(null);

  async function submitTurn({ textOverride, audioBlob }) {
    const textVal = (textOverride ?? inputText).trim();
    if (!textVal && !audioBlob && !attachedFile) return;

    setSending(true);
    setErrorMsg("");
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
      if (attachedFile) {
        fd.append("attachment", attachedFile);
      }

      const res = await apiFetch("/api/agent/turn", {
        method: "POST",
        body: fd,
      });
      setInputText("");
      setAttachedFile(null);
      setLastTurn(res);
      await onAfterAgentMutation();
    } catch (err) {
      setErrorMsg(err.message || "AI request failed");
    } finally {
      setSending(false);
    }
  }

  async function toggleMic() {
    if (recording && mediaRecorderRef.current) {
      mediaRecorderRef.current.stop();
      setRecording(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        await submitTurn({ textOverride: inputText, audioBlob: blob });
      };
      mediaRecorderRef.current = mr;
      mr.start();
      setRecording(true);
    } catch {
      setErrorMsg("Microphone permission denied.");
    }
  }

  async function handleGoalAction(goalId, action, extraBody = null) {
    setSending(true);
    setErrorMsg("");
    try {
      const res = await apiFetch(`/api/agent/goals/${goalId}/${action}`, {
        method: "POST",
        body: extraBody ? JSON.stringify(extraBody) : undefined,
      });
      if (lastTurn?.goals) {
        setLastTurn((prev) => ({
          ...prev,
          goals: prev.goals.map((g) =>
            g.goal_id === goalId ? { ...g, ...res } : g
          ),
        }));
      }
      await onAfterAgentMutation();
    } catch (err) {
      setErrorMsg(err.message || "Action failed");
    } finally {
      setSending(false);
    }
  }

  function renderGoalCard(g) {
    const gid = g.goal_id;
    const choices = g.choices || g.repair_options || [];

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

        <div className="flex items-center gap-2 flex-wrap pt-1">
          {g.status === "completed" && g.undo_available && (
            <button
              onClick={() => handleGoalAction(gid, "undo")}
              className="min-h-[38px] px-3 rounded-lg border border-stone-300 bg-white font-semibold text-stone-800 flex items-center gap-1"
            >
              <Undo2 className="w-3.5 h-3.5" />
              <span>Undo</span>
            </button>
          )}

          {g.status === "awaiting_confirmation" && (
            <>
              <button
                onClick={() => handleGoalAction(gid, "confirm")}
                className="min-h-[40px] px-3.5 rounded-xl bg-emerald-700 text-white font-bold flex items-center gap-1"
              >
                <Check className="w-3.5 h-3.5" />
                <span>Confirm</span>
              </button>
              <button
                onClick={() => handleGoalAction(gid, "park")}
                className="min-h-[40px] px-3 rounded-xl border border-stone-300 bg-white font-semibold text-stone-700"
              >
                Let Clerk Check
              </button>
              <button
                onClick={() => handleGoalAction(gid, "discard")}
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
                      handleGoalAction(gid, "choice", {
                        slot_patch: parsedPatch,
                      })
                    }
                    className="min-h-[40px] px-3 rounded-xl bg-amber-900 text-white font-semibold"
                  >
                    {ch.label}
                  </button>
                );
              })}

              {g.status === "needs_error_fix" && (
                <button
                  onClick={() => handleGoalAction(gid, "park")}
                  className="min-h-[40px] px-3 rounded-xl border border-stone-300 bg-white font-semibold text-stone-800"
                >
                  📥 Let Clerk Fix Later
                </button>
              )}

              {g.status === "parked_for_clerk" && (
                <button
                  onClick={() => handleGoalAction(gid, "confirm")}
                  className="min-h-[40px] px-3 rounded-xl bg-emerald-700 text-white font-bold"
                >
                  Retry Zoho Push
                </button>
              )}

              <button
                onClick={() => handleGoalAction(gid, "discard")}
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
      <div className="fixed bottom-0 inset-x-0 z-40 pointer-events-none">
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

          {/* Latest AI Turn Sheet */}
          {(lastTurn || errorMsg) && (
            <div className="bg-white rounded-2xl border-2 border-amber-300 p-3.5 shadow-xl space-y-2.5 max-h-80 overflow-y-auto">
              <div className="flex items-start justify-between gap-2">
                <div className="space-y-1 text-xs flex-1">
                  {lastTurn?.heard_transcript && (
                    <div className="text-amber-900 font-medium italic">
                      🎙️ Heard: &ldquo;{lastTurn.heard_transcript}&rdquo;
                    </div>
                  )}
                  {errorMsg && (
                    <div className="text-rose-700 font-semibold">{errorMsg}</div>
                  )}
                  {lastTurn?.reply_text && (
                    <div className="text-stone-800 whitespace-pre-line text-sm">
                      {lastTurn.reply_text}
                    </div>
                  )}
                </div>
                <button
                  onClick={() => {
                    setLastTurn(null);
                    setErrorMsg("");
                  }}
                  className="p-1 rounded-lg hover:bg-stone-100"
                >
                  <X className="w-4 h-4 text-stone-500" />
                </button>
              </div>

              {lastTurn?.goals?.length > 0 && (
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
                  ? "Speak or type money/bill update or question (Hinglish)..."
                  : viewingProjectId
                  ? "Update this open order, assign Mistri, or ask AI..."
                  : "Speak or type lead, stage move, Mistri assign, or question..."
              }
              className="flex-1 min-h-[44px] px-2 text-sm outline-none bg-transparent text-stone-900"
            />

            <button
              type="button"
              onClick={toggleMic}
              disabled={sending}
              className={`min-h-[44px] min-w-[44px] rounded-xl flex items-center justify-center transition ${
                recording
                  ? "bg-rose-600 text-white animate-pulse"
                  : "bg-stone-100 text-stone-800 hover:bg-stone-200"
              }`}
            >
              {recording ? (
                <Square className="w-4 h-4" />
              ) : (
                <Mic className="w-5 h-5" />
              )}
            </button>

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
