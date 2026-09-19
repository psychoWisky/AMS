"use client";
import { create } from "zustand";
import { persist } from "zustand/middleware";

// Multi-role/multi-department task (this revision, extending the earlier
// multi-role/role-switching task) — the same role can now repeat across
// departments (e.g. HOD — Agriculture AND HOD — Veterinary), so a flat role
// name is no longer enough to identify "which one" is active. Each entry is
// one persisted backend assignment; `id` is what `POST /auth/switch-role`
// takes (never an independently-chosen role+department pair — the backend
// is the sole authority on which combinations actually exist).
export interface RoleAssignment {
  id: string;
  role: string;
  department_id: string | null;
  department_name: string | null;
}

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
  // only (see backend User.role docstring). `assigned_roles` (distinct role
  // NAMES only, no department detail — kept for back-compat) and
  // `active_role` remain for any not-yet-updated read site, but every
  // role-based UI decision should now read `assigned_role_assignments`/
  // `active_role_assignment_id`/`active_department_id` instead (via
  // `useAssignedRoleAssignments()`/`useActiveDepartmentId()` below), since
  // only those carry the department each assignment actually applies to.
  assigned_roles: string[];
  active_role: string;
  assigned_role_assignments: RoleAssignment[];
  active_role_assignment_id: string | null;
  active_department_id: string | null;
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
// Multi-role/multi-department task (this revision) — full (role, department)
// detail for every assignment the account holds, and which specific one is
// currently active. Prefer these over `useRole()`/`useAssignedRoles()`
// wherever department matters (role switcher, Super Admin assignment UI,
// any "my department" display).
export const useAssignedRoleAssignments = () => useAuthStore((s) => s.user?.assigned_role_assignments ?? []);
export const useActiveRoleAssignmentId = () => useAuthStore((s) => s.user?.active_role_assignment_id ?? null);
export const useActiveDepartmentId = () => useAuthStore((s) => s.user?.active_department_id ?? null);
export const useIsLoggedIn = () => useAuthStore((s) => !!s.user);
export const useSetUser = () => useAuthStore((s) => s.setUser);
