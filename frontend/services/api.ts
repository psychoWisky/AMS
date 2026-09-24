import axios from "axios";

export const api = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001/api/v1",
  headers: { "Content-Type": "application/json" },
});

function getTokens(): { access_token: string | null; refresh_token: string | null } {
  try {
    const raw = localStorage.getItem("ams-auth");
    if (!raw) return { access_token: null, refresh_token: null };
    const parsed = JSON.parse(raw);
    // Zustand persist wraps state: { state: { access_token, ... }, version: 0 }
    const state = parsed?.state ?? parsed;
    return { access_token: state?.access_token ?? null, refresh_token: state?.refresh_token ?? null };
  } catch {
    return { access_token: null, refresh_token: null };
  }
}

function saveAccessToken(newAccessToken: string) {
  try {
    const raw = localStorage.getItem("ams-auth");
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed?.state) {
      parsed.state.access_token = newAccessToken;
    } else {
      parsed.access_token = newAccessToken;
    }
    localStorage.setItem("ams-auth", JSON.stringify(parsed));
  } catch {}
}

api.interceptors.request.use((cfg) => {
  if (typeof window !== "undefined") {
    const { access_token } = getTokens();
    if (access_token) cfg.headers.Authorization = `Bearer ${access_token}`;
  }
  return cfg;
});

api.interceptors.response.use(
  (res) => res,
  async (err) => {
    if (err.response?.status === 401 && typeof window !== "undefined") {
      const { refresh_token } = getTokens();
      if (refresh_token) {
        try {
          const res = await axios.post(`${api.defaults.baseURL}/auth/refresh`, { refresh_token });
          const { access_token: newToken } = res.data;
          saveAccessToken(newToken);
          err.config.headers.Authorization = `Bearer ${newToken}`;
          return api.request(err.config);
        } catch {
          localStorage.removeItem("ams-auth");
          window.location.href = "/login";
        }
      }
    }
    return Promise.reject(err);
  }
);

// Every AMS document/PDF endpoint (Thesis, Migration, Progress Report, ...) requires the same
// Bearer-token authentication as every other API call — there is no cookie-based auth anywhere
// in this app. A plain `window.open(apiUrl, "_blank")` opens a brand-new, unauthenticated
// browser navigation that never passes through this file's request interceptor, so the backend
// correctly (and necessarily) rejects it with 401 "Not authenticated". This helper is the fix:
// fetch the file through the authenticated `api` client as a Blob, then open THAT object URL
// (which carries no auth requirement of its own — the browser already has the bytes) in a new
// tab, preserving the existing "opens in a new tab to view/print" UX exactly. Reuse this for
// every View/Print action instead of building a raw URL — never re-introduce the broken pattern.
export async function viewFileInNewTab(path: string, params?: Record<string, string>): Promise<void> {
  const res = await api.get(path, { params, responseType: "blob" });
  const url = window.URL.createObjectURL(res.data);
  window.open(url, "_blank");
  // Revoked well after the new tab has had time to load the blob — revoking immediately can
  // race the new tab's own fetch of the object URL in some browsers.
  setTimeout(() => window.URL.revokeObjectURL(url), 60_000);
}

// `responseType: "blob"` means an error response body (e.g. the JSON {"detail": "..."} from a
// 403/404) arrives as a Blob too, not parsed JSON — this decodes it back to the same shape
// `apiErrorMessage` already expects, so every blob-fetching action gets the real backend message
// instead of a generic fallback.
export async function blobErrorMessage(e: unknown, fallback: string): Promise<string> {
  const data = (e as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text());
      if (typeof parsed?.detail === "string") return parsed.detail;
    } catch { /* not JSON — keep the fallback */ }
  }
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}
