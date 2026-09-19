"use client";
import { useState, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useIsLoggedIn, useUser } from "@/stores/auth.store";
import { AMSSidebar } from "@/components/layouts/sidebar";
import { AccessDenied } from "@/components/ui/access-denied";
import { canAccessRoute } from "@/lib/navigation";
import { cn } from "@/lib/utils";

// BUSINESS_LOGIC.md Section N (student profile-completion gating) — routes a
// student with an incomplete mandatory profile may still reach directly (via
// sidebar or URL); everything else redirects to Student Management. This is
// the single, centralized enforcement point for the "sidebar stays visible,
// but clicking a restricted module must not bypass the gate" requirement —
// no per-page/per-route guard hacks.
const STUDENT_PROFILE_GATE_ALLOWLIST = ["/dashboard", "/student-management", "/notifications"];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const isLoggedIn = useIsLoggedIn();
  const user = useUser();
  const router = useRouter();
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => { setMounted(true); }, []);

  useEffect(() => {
    if (mounted && !isLoggedIn) router.replace("/login");
  }, [mounted, isLoggedIn, router]);

  // Multi-role/role-switching task — gated on the session's ACTIVE role, not
  // the legacy primary `role` field, so a (currently hypothetical) student
  // who also holds another role is only gated while actually acting as a
  // student; switching away from Student mode lifts the gate, exactly as it
  // would for a single-role student switching back would re-apply it.
  const activeRole = user?.active_role ?? user?.role;

  useEffect(() => {
    if (!mounted || !user) return;
    if (activeRole !== "student" || user.profile_complete) return;
    if (!STUDENT_PROFILE_GATE_ALLOWLIST.some((p) => pathname.startsWith(p))) {
      router.replace("/student-management");
    }
  }, [mounted, user, activeRole, pathname, router]);

  if (!mounted || !isLoggedIn) return null;
  if (user && activeRole === "student" && !user.profile_complete && !STUDENT_PROFILE_GATE_ALLOWLIST.some((p) => pathname.startsWith(p))) {
    return null;
  }

  // Route guard (UX only — the backend authorizes every request itself).
  // Decided from the session's ACTIVE role, never from the set of roles the
  // account merely holds elsewhere, so an inactive assignment grants nothing.
  // The page component is not mounted at all when denied, so it fires none
  // of its role-restricted requests.
  const routeAllowed = canAccessRoute(pathname, activeRole);

  return (
    <div className="min-h-screen bg-[#F5F7FA] text-gray-900">
      <AMSSidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <main className={cn("transition-all duration-200 min-h-screen", collapsed ? "ml-16" : "ml-[220px]")}>
        {routeAllowed ? children : <AccessDenied />}
      </main>
    </div>
  );
}
