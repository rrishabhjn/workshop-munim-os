import { useEffect, useState } from "react";
import {
  Bell,
  ClipboardList,
  FolderKanban,
  LogOut,
  Users,
} from "lucide-react";
import {
  apiFetch,
  getSupabaseClient,
  getTelegramInitData,
  initSupabaseClient,
} from "./lib/api";
import WhosDoingWhat from "./components/WhosDoingWhat";
import AllOrdersBoard from "./components/AllOrdersBoard";
import RosterDrawer from "./components/RosterDrawer";
import MunimPortal from "./components/MunimPortal";
import AgentCommandBar from "./components/AgentCommandBar";

function detectInitialDomain() {
  if (typeof window !== "undefined" && window.location.pathname.startsWith("/munim")) {
    return "munim";
  }
  return "production";
}

export default function App() {
  const [authChecked, setAuthChecked] = useState(false);
  const [sbClient, setSbClient] = useState(() => getSupabaseClient());
  const [domain, setDomain] = useState(detectInitialDomain);
  const [prodTab, setProdTab] = useState("whos_doing_what");
  const [prodData, setProdData] = useState(null);
  const [munimData, setMunimData] = useState(null);
  const [selectedOrderId, setSelectedOrderId] = useState(null);
  const [showRoster, setShowRoster] = useState(false);
  const [showNeedsInfo, setShowNeedsInfo] = useState(false);
  const [undoToast, setUndoToast] = useState(null);
  const [error, setError] = useState("");

  async function loadAllData() {
    try {
      setError("");
      const [pOverview, mOverview] = await Promise.all([
        apiFetch("/api/production/overview"),
        apiFetch("/api/munim/overview"),
      ]);
      setProdData(pOverview);
      setMunimData(mOverview);
    } catch (err) {
      setProdData(null);
      setError(err.message || "Authentication required");
    } finally {
      setAuthChecked(true);
    }
  }

  async function refreshActiveDomain(targetDomain = domain) {
    if (targetDomain === "production") {
      const pOverview = await apiFetch("/api/production/overview");
      setProdData(pOverview);
      apiFetch("/api/munim/overview")
        .then(setMunimData)
        .catch(() => {});
    } else {
      const mOverview = await apiFetch("/api/munim/overview");
      setMunimData(mOverview);
      apiFetch("/api/production/overview")
        .then(setProdData)
        .catch(() => {});
    }
  }

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    if (tg) {
      tg.ready();
      tg.expand();
    }

    if (getTelegramInitData()) {
      loadAllData();
      return;
    }

    let authSub = null;
    let cancelled = false;

    initSupabaseClient().then((sb) => {
      if (cancelled) return;
      setSbClient(sb);
      if (!sb) {
        setAuthChecked(true);
        return;
      }
      sb.auth.getSession().then(({ data }) => {
        if (cancelled) return;
        if (data?.session) {
          loadAllData();
        } else {
          setAuthChecked(true);
        }
      });
      const { data: sub } = sb.auth.onAuthStateChange((_ev, session) => {
        if (session) loadAllData();
        else {
          setProdData(null);
          setAuthChecked(true);
        }
      });
      authSub = sub?.subscription || null;
    });

    return () => {
      cancelled = true;
      if (authSub) authSub.unsubscribe();
    };
  }, []);

  function switchDomain(nextDomain) {
    setDomain(nextDomain);
    const targetPath = nextDomain === "munim" ? "/munim" : "/production";
    if (window.location.pathname !== targetPath) {
      window.history.pushState({}, "", targetPath);
    }
  }

  async function handleGoogleLogin() {
    if (!sbClient) return;
    await sbClient.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}${window.location.pathname}` },
    });
  }

  async function handleLogout() {
    if (sbClient) await sbClient.auth.signOut();
    setProdData(null);
  }

  function showUndoBanner(message, eventIds) {
    setUndoToast({ message, eventIds });
  }

  async function handleUndoEvents(eventIds) {
    await apiFetch("/api/production/undo", {
      method: "POST",
      body: JSON.stringify({ event_ids: eventIds }),
    });
    setUndoToast(null);
    await loadAllData();
  }

  async function handleQuickAdvanceFromQueue(workRow, nextCode) {
    let res = null;
    if (workRow.work_kind === "project_pipeline") {
      res = await apiFetch(
        `/api/production/orders/${workRow.project_id}/pipeline`,
        {
          method: "POST",
          body: JSON.stringify({ to_stage: nextCode }),
        }
      );
    } else if (workRow.work_kind === "material_track") {
      res = await apiFetch(
        `/api/production/orders/${workRow.project_id}/material`,
        {
          method: "POST",
          body: JSON.stringify({ to_stage: nextCode }),
        }
      );
    } else if (workRow.work_kind === "item_execution" && workRow.item_id) {
      res = await apiFetch(`/api/production/items/${workRow.item_id}`, {
        method: "PATCH",
        body: JSON.stringify({ to_stage: nextCode }),
      });
    }
    await loadAllData();
    if (res?.undo_event_ids?.length) {
      showUndoBanner(res.receipt || "Updated", res.undo_event_ids);
    }
  }

  if (!authChecked) {
    return (
      <div className="min-h-screen flex items-center justify-center text-stone-500">
        Loading Workshop OS...
      </div>
    );
  }

  if (!prodData?.me) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4 bg-stone-100">
        <div className="bg-white max-w-sm w-full rounded-2xl border border-stone-200 p-6 text-center space-y-4 shadow-sm">
          <h1 className="text-2xl font-extrabold text-amber-950">
            Workshop Munim OS
          </h1>
          <p className="text-sm text-stone-600">
            Beawar Production Lifecycle &amp; Management Ledger
          </p>
          {error && (
            <div className="text-xs bg-rose-50 text-rose-800 border border-rose-200 rounded-xl p-2.5">
              {error}
            </div>
          )}
          {sbClient ? (
            <button
              onClick={handleGoogleLogin}
              className="w-full min-h-[48px] rounded-xl bg-amber-900 text-white font-semibold shadow-sm"
            >
              Sign in with Google
            </button>
          ) : (
            <p className="text-xs text-stone-500">
              Open inside Telegram Mini App, or set VITE_SUPABASE_URL &amp;
              VITE_SUPABASE_PUBLISHABLE_KEY for browser login.
            </p>
          )}
        </div>
      </div>
    );
  }

  const needsInfoList = prodData.needs_info || [];

  return (
    <div className="min-h-screen bg-stone-100 text-stone-900">
      {/* Top Header with Domain Switcher (/production vs /munim) + Needs Info Bell */}
      <header className="sticky top-0 z-30 bg-white/95 backdrop-blur border-b border-stone-200">
        <div className="max-w-4xl mx-auto px-3 py-2.5 flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 bg-stone-100 p-1 rounded-xl">
            <button
              onClick={() => switchDomain("production")}
              className={`min-h-[38px] px-3 rounded-lg text-xs font-bold transition ${
                domain === "production"
                  ? "bg-amber-900 text-white shadow-sm"
                  : "text-stone-600 hover:text-stone-900"
              }`}
            >
              🏭 Production
            </button>
            <button
              onClick={() => switchDomain("munim")}
              className={`min-h-[38px] px-3 rounded-lg text-xs font-bold transition ${
                domain === "munim"
                  ? "bg-amber-900 text-white shadow-sm"
                  : "text-stone-600 hover:text-stone-900"
              }`}
            >
              📒 Munim
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            {domain === "production" && (
              <button
                onClick={() => setShowRoster(true)}
                className="min-h-[40px] px-3 rounded-xl border border-stone-200 bg-white text-xs font-semibold text-stone-700 flex items-center gap-1.5"
              >
                <Users className="w-4 h-4" />
                <span className="hidden sm:inline">Roster</span>
              </button>
            )}

            <button
              onClick={() => setShowNeedsInfo(true)}
              className={`min-h-[40px] px-3 rounded-xl border text-xs font-bold flex items-center gap-1.5 ${
                needsInfoList.length > 0
                  ? "bg-amber-100 border-amber-400 text-amber-950"
                  : "bg-white border-stone-200 text-stone-600"
              }`}
            >
              <Bell className="w-4 h-4" />
              <span>Needs Info ({needsInfoList.length})</span>
            </button>

            {prodData.me.auth_type === "supabase" && (
              <button
                onClick={handleLogout}
                title="Sign out"
                className="min-h-[40px] min-w-[40px] flex items-center justify-center rounded-xl text-stone-500 hover:bg-stone-100"
              >
                <LogOut className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>

        {/* Production 2-Screen Sub-Nav */}
        {domain === "production" && (
          <div className="max-w-4xl mx-auto px-3 pb-2 flex items-center gap-2">
            <button
              onClick={() => setProdTab("whos_doing_what")}
              className={`flex-1 min-h-[42px] rounded-xl text-xs font-bold flex items-center justify-center gap-1.5 transition ${
                prodTab === "whos_doing_what"
                  ? "bg-stone-900 text-white"
                  : "bg-stone-100 text-stone-700"
              }`}
            >
              <ClipboardList className="w-4 h-4" />
              <span>Who&apos;s Doing What</span>
            </button>
            <button
              onClick={() => setProdTab("all_orders")}
              className={`flex-1 min-h-[42px] rounded-xl text-xs font-bold flex items-center justify-center gap-1.5 transition ${
                prodTab === "all_orders"
                  ? "bg-stone-900 text-white"
                  : "bg-stone-100 text-stone-700"
              }`}
            >
              <FolderKanban className="w-4 h-4" />
              <span>All Orders ({(prodData.orders || []).length})</span>
            </button>
          </div>
        )}
      </header>

      {/* Main Content */}
      <main className="max-w-4xl mx-auto px-3 pt-4">
        {domain === "production" ? (
          prodTab === "whos_doing_what" ? (
            <WhosDoingWhat
              me={prodData.me}
              people={prodData.people}
              whosDoingWhat={prodData.whos_doing_what}
              onQuickAdvance={handleQuickAdvanceFromQueue}
              onOpenOrder={(projectId) => {
                setSelectedOrderId(projectId);
                setProdTab("all_orders");
              }}
            />
          ) : (
            <AllOrdersBoard
              lookups={prodData.lookups}
              people={prodData.people}
              orders={prodData.orders}
              selectedOrderId={selectedOrderId}
              onSelectOrder={setSelectedOrderId}
              onMutated={loadAllData}
              onShowUndoToast={showUndoBanner}
            />
          )
        ) : (
          <MunimPortal munimData={munimData} onRefresh={loadAllData} />
        )}
      </main>

      {/* Roster Slide-Over */}
      <RosterDrawer
        open={showRoster}
        onClose={() => setShowRoster(false)}
        people={prodData.people}
        onSaved={loadAllData}
      />

      {/* Persistent Omnichannel AI Command Bar + Undo Toast + Needs Info Drawer */}
      <AgentCommandBar
        domainScope={domain}
        viewingProjectId={
          domain === "production" && prodTab === "all_orders"
            ? selectedOrderId
            : null
        }
        paymentAccounts={munimData?.payment_accounts || []}
        needsInfo={needsInfoList}
        showNeedsInfo={showNeedsInfo}
        onCloseNeedsInfo={() => setShowNeedsInfo(false)}
        undoToast={undoToast}
        onDismissUndoToast={() => setUndoToast(null)}
        onTriggerUndoEvents={handleUndoEvents}
        onAfterAgentMutation={() => refreshActiveDomain(domain)}
        onViewOrder={(projectId) => {
          switchDomain("production");
          setProdTab("all_orders");
          setSelectedOrderId(projectId);
        }}
      />
    </div>
  );
}
