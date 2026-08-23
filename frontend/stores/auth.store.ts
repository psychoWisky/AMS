"use client";
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthUser {
  id: string;
  email: string;
  full_name: string;
  role: string;
  designation: string | null;
  department_id: string | null;
  program_id: string | null;
}

interface AuthState {
  user: AuthUser | null;
  access_token: string | null;
  refresh_token: string | null;
  setAuth: (user: AuthUser, access_token: string, refresh_token: string) => void;
  clearAuth: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      access_token: null,
      refresh_token: null,
      setAuth: (user, access_token, refresh_token) => set({ user, access_token, refresh_token }),
      clearAuth: () => set({ user: null, access_token: null, refresh_token: null }),
    }),
    { name: "ams-auth" }
  )
);

export const useUser = () => useAuthStore((s) => s.user);
export const useRole = () => useAuthStore((s) => s.user?.role ?? null);
export const useIsLoggedIn = () => useAuthStore((s) => !!s.user);
