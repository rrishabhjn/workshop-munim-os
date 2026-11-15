import { useEffect, useState } from "react";
import { LayoutGrid, Columns3, ShoppingCart, FolderKanban, LogOut } from "lucide-react";
import { apiFetch, getTelegramInitData, supabase } from "./lib/api";
import ProjectsBoard from "./components/ProjectsBoard";
import ProjectDetail from "./components/ProjectDetail";
import KanbanBoard from "./components/KanbanBoard";
import PurchaseList from "./components/PurchaseList";
import BackOffice from "./components/BackOffice";

export default function App() {
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [tab, setTab] = useState("projects");
  const [selectedProjectId, setSelectedProjectId] = useState(null);
  const [projects, setProjects] = useState([]);
  const [stages, setStages] = useState([]);
  const [categories, setCategories] = useState([]);
  const [error, setError] = useState("");

  async function loadCoreData() {
    try {
      setError("");
      const [me, prjs, stgs, cats] = await Promise.all([
        apiFetch("/api/me"),
        apiFetch("/api/projects"),
        apiFetch("/api/stages"),
        apiFetch("/api/material-categories"),
      ]);
      setUser(me);
      setProjects(prjs);
      setStages(stgs);
      setCategories(cats);
    } catch (err) {
      setUser(null);
      setError(err.message || "Authentication required");
    } finally {
      setAuthChecked(true);
    }
  }

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    if (tg) {
      tg.ready();
      tg.expand();
    }

    if (getTelegramInitData()) {
      loadCoreData();
      return;
    }

    if (supabase) {
      supabase.auth.getSession().then(({ data }) => {
        if (data?.session) {
          loadCoreData();
        } else {
          setAuthChecked(true);
        }
      });
      const { data: sub } = supabase.auth.onAuthStateChange((_ev, session) => {
        if (session) loadCoreData();
        else {
          setUser(null);
          setAuthChecked(true);
        }
      });
      return () => sub.subscription.unsubscribe();
    } else {
      setAuthChecked(true);
    }
  }, []);

  async function handleGoogleLogin() {
    if (!supabase) return;
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
  }

  async function handleLogout() {
    if (supabase) await supabase.auth.signOut();
    setUser(null);
  }

  if (!authChecked) {
    return (
      <div className="min-h-screen flex items-center justify-center text-stone-500">
        Loading Workshop Munim OS...
      </div>
    );
  }

  if (!user) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4 bg-stone-100">
        <div className="bg-white max-w-sm w-full rounded-2xl border border-stone-200 p-6 text-center space-y-4 shadow-sm">
          <h1 className="text-2xl font-extrabold text-amber-950">Workshop Munim OS</h1>
          <p className="text-sm text-stone-600">
            Beawar Custom Furniture Operations &amp; Bookkeeping
          </p>
          {error && (
            <div className="text-xs bg-rose-50 text-rose-800 border border-rose-200 rounded-xl p-2.5">
              {error}
            </div>
          )}
          {supabase ? (
            <button
              onClick={handleGoogleLogin}
              className="w-full py-3 rounded-xl bg-amber-900 text-white font-semibold shadow-sm"
            >
              Sign in with Google
            </button>
          ) : (
            <p className="text-xs text-stone-500">
              Open this Mini App inside Telegram, or configure VITE_SUPABASE_URL &amp;
              VITE_SUPABASE_PUBLISHABLE_KEY for browser sign-in.
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen pb-24">
      <header className="bg-amber-950 text-amber-50 px-4 py-3 sticky top-0 z-30 shadow-sm">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="font-extrabold text-base tracking-tight">Workshop Munim OS</h1>
            <p className="text-[11px] text-amber-200">
              {user.display_name} ({user.auth_type})
            </p>
          </div>
          {user.auth_type === "supabase" && (
            <button
              onClick={handleLogout}
              className="p-2 rounded-xl text-amber-200 hover:bg-amber-900"
              title="Sign out"
            >
              <LogOut className="w-4 h-4" />
            </button>
          )}
        </div>
      </header>

      <main className="max-w-5xl mx-auto p-4">
        {selectedProjectId ? (
          <ProjectDetail
            projectId={selectedProjectId}
            stages={stages}
            categories={categories}
            onBack={() => setSelectedProjectId(null)}
            onRefresh={loadCoreData}
          />
        ) : (
          <>
            {tab === "projects" && (
              <ProjectsBoard
                projects={projects}
                stages={stages}
                onSelectProject={setSelectedProjectId}
                onRefresh={loadCoreData}
              />
            )}
            {tab === "kanban" && (
              <KanbanBoard
                projects={projects}
                stages={stages}
                onSelectProject={(pid) => setSelectedProjectId(pid)}
                onRefresh={loadCoreData}
              />
            )}
            {tab === "purchase" && <PurchaseList />}
            {tab === "backoffice" && <BackOffice />}
          </>
        )}
      </main>

      <nav className="fixed bottom-0 inset-x-0 bg-white border-t border-stone-200 z-30">
        <div className="max-w-5xl mx-auto grid grid-cols-4">
          {[
            { id: "projects", label: "Projects", icon: LayoutGrid },
            { id: "kanban", label: "Kanban", icon: Columns3 },
            { id: "purchase", label: "Purchase", icon: ShoppingCart },
            { id: "backoffice", label: "Munim", icon: FolderKanban },
          ].map((item) => {
            const Icon = item.icon;
            const active = !selectedProjectId && tab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => {
                  setSelectedProjectId(null);
                  setTab(item.id);
                }}
                className={`flex flex-col items-center justify-center py-2.5 text-xs font-medium ${
                  active ? "text-amber-900 font-bold" : "text-stone-500"
                }`}
              >
                <Icon className="w-5 h-5 mb-0.5" />
                {item.label}
              </button>
            );
          })}
        </div>
      </nav>
    </div>
  );
}
