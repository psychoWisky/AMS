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
  // Multi-role/role-switching task — `role` is retained for legacy display
  // only (see backend User.role docstring). `assigned_roles` is every role
  // a Super Admin has granted this account; `active_role` is which one is
  // currently in effect for THIS session (server-authoritative — see
  // POST /auth/switch-role). Every role-based UI decision should read
  // `active_role` (via `useRole()` below), never the legacy `role` field.
  assigned_roles: string[];
  active_role: string;
  designation: string | null;
  department_id: string | null;
  college_id: string | null;
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
// Multi-role/role-switching task — every existing caller of useRole() gets
// the session's ACTIVE role (never the legacy primary `role` field), which
// is exactly what should drive nav filtering/page gates. Falls back to
// `role` only for a user object fetched before this field existed.
export const useRole = () => useAuthStore((s) => s.user?.active_role ?? s.user?.role ?? null);
export const useAssignedRoles = () => useAuthStore((s) => s.user?.assigned_roles ?? (s.user ? [s.user.role] : []));
export const useIsLoggedIn = () => useAuthStore((s) => !!s.user);
export const useSetUser = () => useAuthStore((s) => s.setUser);
