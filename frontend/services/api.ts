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
