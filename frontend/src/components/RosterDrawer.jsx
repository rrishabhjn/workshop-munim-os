import { useState } from "react";
import { X, UserPlus } from "lucide-react";
import { apiFetch } from "../lib/api";

export default function RosterDrawer({ open, onClose, people, onSaved }) {
  const [fullName, setFullName] = useState("");
  const [personType, setPersonType] = useState("salaried_karigar");
  const [roleTag, setRoleTag] = useState("mistri");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [aliases, setAliases] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  if (!open) return null;

  async function handleSubmit(e) {
    e.preventDefault();
    if (!fullName.trim()) return;
    setSaving(true);
    setErr("");
    try {
      await apiFetch("/api/munim/people", {
        method: "POST",
        body: JSON.stringify({
          full_name: fullName.trim(),
          person_type: personType,
          role_tags: [roleTag],
          phone: phone.trim() || null,
          email: personType === "operator" && email.trim() ? email.trim() : null,
          aliases: aliases
            .split(",")
            .map((a) => a.trim())
            .filter(Boolean),
        }),
      });
      setFullName("");
      setPhone("");
      setEmail("");
      setAliases("");
      await onSaved();
    } catch (error) {
      setErr(error.message || "Failed adding person");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex justify-end">
      <div className="bg-white w-full max-w-md h-full flex flex-col shadow-2xl">
        <div className="p-4 border-b border-stone-200 flex items-center justify-between">
          <div>
            <h2 className="font-bold text-base text-stone-900">
              Team &amp; Mistri Roster
            </h2>
            <p className="text-xs text-stone-500">
              Office Operators log in; Mistris stay offline &amp; get WhatsApp Job Cards
            </p>
          </div>
          <button
            onClick={onClose}
            className="min-h-[44px] min-w-[44px] flex items-center justify-center rounded-xl hover:bg-stone-100"
          >
            <X className="w-5 h-5 text-stone-600" />
          </button>
        </div>

        <div className="p-4 overflow-y-auto flex-1 space-y-5">
          <form
            onSubmit={handleSubmit}
            className="bg-stone-50 rounded-2xl border border-stone-200 p-3.5 space-y-3"
          >
            <div className="flex items-center gap-1.5 text-xs font-bold text-amber-950 uppercase">
              <UserPlus className="w-4 h-4" />
              <span>Add Person / Mistri</span>
            </div>
            {err && (
              <div className="text-xs text-rose-700 bg-rose-50 p-2 rounded-lg">
                {err}
              </div>
            )}
            <input
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Full Name (e.g. Ramesh Mistri, Rahul)"
              className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
              required
            />
            <div className="grid grid-cols-2 gap-2">
              <select
                value={personType}
                onChange={(e) => {
                  setPersonType(e.target.value);
                  setRoleTag(
                    e.target.value === "operator" ? "estimator" : "mistri"
                  );
                }}
                className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 text-sm bg-white"
              >
                <option value="salaried_karigar">Shop/Site Mistri (Offline)</option>
                <option value="contractor_mistri">Contractor Mistri (Offline)</option>
                <option value="operator">Office Operator (Logs In)</option>
              </select>
              <select
                value={roleTag}
                onChange={(e) => setRoleTag(e.target.value)}
                className="min-h-[44px] px-2.5 rounded-xl border border-stone-300 text-sm bg-white"
              >
                <option value="mistri">Mistri / Karigar</option>
                <option value="estimator">Estimator</option>
                <option value="purchase">Inventory / Purchase</option>
                <option value="qc">QC / Head Karigar</option>
                <option value="clerk">Clerk / Munim</option>
              </select>
            </div>
            <input
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="WhatsApp Phone (optional)"
              className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
            />
            {personType === "operator" && (
              <input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Google Login Email (optional)"
                className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
              />
            )}
            <input
              value={aliases}
              onChange={(e) => setAliases(e.target.value)}
              placeholder="Nicknames / Voice Aliases (comma-separated, e.g. Ramu)"
              className="w-full min-h-[44px] px-3 rounded-xl border border-stone-300 text-sm"
            />
            <button
              type="submit"
              disabled={saving}
              className="w-full min-h-[44px] rounded-xl bg-amber-900 text-white font-semibold text-sm shadow-sm disabled:opacity-50"
            >
              {saving ? "Saving..." : "+ Add to Roster"}
            </button>
          </form>

          <div className="space-y-2">
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-500">
              Active Roster ({(people || []).length})
            </h3>
            {(people || []).map((p) => (
              <div
                key={p.id}
                className="p-3 rounded-xl border border-stone-200 flex items-center justify-between text-sm"
              >
                <div>
                  <div className="font-semibold text-stone-900">{p.full_name}</div>
                  <div className="text-xs text-stone-500">
                    {p.person_type === "operator" ? "🟢 App Operator" : "🔨 Offline Mistri"} •{" "}
                    {(p.role_tags || []).join(", ")}
                    {p.aliases?.length ? ` • aka ${p.aliases.join(", ")}` : ""}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
