"use client";
import { useState, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useIsLoggedIn, useUser } from "@/stores/auth.store";
import { AMSSidebar } from "@/components/layouts/sidebar";
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

  useEffect(() => {
    if (!mounted || !user) return;
    if (user.role !== "student" || user.profile_complete) return;
    if (!STUDENT_PROFILE_GATE_ALLOWLIST.some((p) => pathname.startsWith(p))) {
      router.replace("/student-management");
    }
  }, [mounted, user, pathname, router]);

  if (!mounted || !isLoggedIn) return null;
  if (user && user.role === "student" && !user.profile_complete && !STUDENT_PROFILE_GATE_ALLOWLIST.some((p) => pathname.startsWith(p))) {
    return null;
  }

  return (
    <div className="min-h-screen bg-[#F5F7FA] text-gray-900">
      <AMSSidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <main className={cn("transition-all duration-200 min-h-screen", collapsed ? "ml-16" : "ml-[220px]")}>
        {children}
      </main>
    </div>
  );
}
