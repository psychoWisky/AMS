"use client";
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthUser {
  id: string;
  email: string;
  full_name: string;
  title: string | null;
  first_name: string | null;
  middle_name: string | null;
  last_name: string | null;
  mobile: string | null;
  role: string;
  designation: string | null;
  department_id: string | null;
  program_id: string | null;
  student_roll: string | null;
  date_of_birth: string | null;
  gender: string | null;
  blood_group: string | null;
  father_name: string | null;
  abc_id: string | null;
  address: string | null;
  must_change_password: boolean;
  profile_complete: boolean;
  missing_profile_fields: string[];
}

interface AuthState {
  user: AuthUser | null;
  access_token: string | null;
  refresh_token: string | null;
  setAuth: (user: AuthUser, access_token: string, refresh_token: string) => void;
  setUser: (user: AuthUser) => void;
  clearAuth: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      access_token: null,
      refresh_token: null,
      setAuth: (user, access_token, refresh_token) => set({ user, access_token, refresh_token }),
      // Refreshes the cached user (e.g. after PATCH /auth/me) without touching tokens.
      setUser: (user) => set({ user }),
      clearAuth: () => set({ user: null, access_token: null, refresh_token: null }),
    }),
    { name: "ams-auth" }
  )
);

export const useUser = () => useAuthStore((s) => s.user);
export const useRole = () => useAuthStore((s) => s.user?.role ?? null);
export const useIsLoggedIn = () => useAuthStore((s) => !!s.user);
export const useSetUser = () => useAuthStore((s) => s.setUser);
