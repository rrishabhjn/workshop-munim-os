import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL || "";
const SUPABASE_PUBLISHABLE_KEY =
  import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY || "";

export let supabase =
  SUPABASE_URL && SUPABASE_PUBLISHABLE_KEY
    ? createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)
    : null;

let initPromise = null;

export function getSupabaseClient() {
  return supabase;
}

export async function initSupabaseClient() {
  if (supabase) return supabase;
  if (!initPromise) {
    initPromise = fetch("/api/public-config")
      .then((r) => (r.ok ? r.json() : null))
      .then((cfg) => {
        if (cfg?.supabase_url && cfg?.supabase_publishable_key) {
          supabase = createClient(
            cfg.supabase_url,
            cfg.supabase_publishable_key
          );
        }
        return supabase;
      })
      .catch(() => null);
  }
  return initPromise;
}

export function getTelegramInitData() {
  return window.Telegram?.WebApp?.initData || "";
}

async function getAuthHeaders() {
  const tgInit = getTelegramInitData();
  if (tgInit) {
    return { "X-Telegram-Init-Data": tgInit };
  }
  const sb = supabase || (await initSupabaseClient());
  if (sb) {
    const { data } = await sb.auth.getSession();
    const token = data?.session?.access_token;
    if (token) {
      return { Authorization: `Bearer ${token}` };
    }
  }
  return {};
}

export async function apiFetch(path, options = {}) {
  const authHeaders = await getAuthHeaders();
  const isFormData = options.body instanceof FormData;
  const headers = {
    ...authHeaders,
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
    ...(options.headers || {}),
  };

  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(errText || `HTTP ${resp.status}`);
  }
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return resp.json();
  }
  return resp.text();
}
