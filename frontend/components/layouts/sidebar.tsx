"use client";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useRole } from "@/stores/auth.store";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard, CalendarDays, BookOpen, Users, ClipboardList,
  BarChart3, FlaskConical, Bell, Settings, ChevronLeft, ChevronRight, GraduationCap, LogOut, FileText, ClipboardCheck, IdCard, UserCog, ShieldCheck, FileSpreadsheet, KeyRound,
} from "lucide-react";
import { useAuthStore } from "@/stores/auth.store";
import { api } from "@/services/api";
import { useState } from "react";
import { ChangePasswordModal } from "@/components/ui/change-password-modal";

const NAV = [
  { label: "Dashboard",       icon: LayoutDashboard, href: "/dashboard",    roles: [] },
  { label: "Academic Calendar",icon: CalendarDays,   href: "/calendar",     roles: ["super_admin","academic_admin","registrar","hod","faculty","student","examiner"] },
  // "faculty" deliberately excluded here (BUSINESS_LOGIC.md Section N — Faculty
  // has no need for a generic all-courses catalog; their course-relevant view
  // is "Teacher Courses" (their own assigned offerings) below.
  // "student" also excluded (Student/Faculty module-cleanup task, this
  // revision) — the generic Course Catalogue is not a confirmed student
  // module (absent from BUSINESS_LOGIC.md K.7); students see eligible
  // offered courses via Course Registration and their own history via My
  // Courses. The `GET /courses`/`GET /courses/offerings/all` backend
  // endpoints and this route are UNCHANGED — Super Admin/Academic
  // Admin/HOD still need them for Course Management exactly as before.
  { label: "Courses",         icon: BookOpen,        href: "/courses",      roles: ["super_admin","academic_admin","hod"] },
  // "hod" deliberately excluded here (BUSINESS_LOGIC.md Section N.1) — HOD's
  // course-offering management already lives at Courses -> Offer Course;
  // this page is instructor-assigned "my courses" and does not apply to HOD.
  { label: "Teacher Courses", icon: BookOpen,        href: "/teacher-courses", roles: ["super_admin","academic_admin","faculty","registrar"] },
  { label: "Faculties",       icon: UserCog,         href: "/faculties",    roles: ["hod"] },
  // "student" deliberately excluded here — Course Registration is now the
  // single student-facing course selection/enrollment workflow; this page
  // remains staff-only "Enrollment Management" (review/approve per-offering
  // requests) for every other role.
  // "faculty" also excluded (Student/Faculty module-cleanup task, this
  // revision) — this legacy, non-registration-stage-aware view is fully
  // superseded for Faculty by "Course Request" below (the confirmed module
  // per BUSINESS_LOGIC.md M.9, already correctly scoped to the faculty's own
  // assigned offerings). Kept for Super Admin/Academic Admin/HOD/Registrar,
  // who may still legitimately use it as a cross-department administrative
  // tool — the route/page/backend are unchanged.
  { label: "Enrollment",      icon: ClipboardList,   href: "/enrollment",   roles: ["super_admin","academic_admin","hod","registrar"] },
  // My Courses task (this revision) — same route (`/enrollment`), a
  // separate nav entry so students see the confirmed "My Courses" label
  // (BUSINESS_LOGIC.md K.7) instead of the staff-facing "Enrollment" label;
  // the route itself branches on role internally, unchanged.
  { label: "My Courses",      icon: BookOpen,        href: "/enrollment",   roles: ["student"] },
  { label: "Course Registration",icon: ClipboardCheck,href: "/course-registration", roles: ["student"] },
  { label: "Course Request",  icon: ClipboardList,   href: "/course-request", roles: ["super_admin","academic_admin","hod","faculty","research_supervisor"] },
  { label: "Academic Progress",icon: GraduationCap,  href: "/academic-progress", roles: ["super_admin","academic_admin","hod","faculty","student","registrar","research_supervisor"] },
  { label: "Grading",         icon: BarChart3,       href: "/grading",      roles: ["super_admin","academic_admin","hod","faculty","registrar","examiner"] },
  { label: "Admit Card",      icon: FileText,        href: "/admit-card",   roles: ["super_admin","academic_admin","hod","registrar","examiner","student"] },
  // Advisory Committee naming task (this revision) — visible label only;
  // the route (/research), page component, and API endpoints are unchanged.
  { label: "Advisory Committee", icon: FlaskConical,  href: "/research",     roles: ["super_admin","academic_admin","hod","faculty","student","research_supervisor"] },
  { label: "Admissions",      icon: ClipboardCheck,  href: "/admissions",   roles: ["super_admin","academic_admin","registrar"] },
  { label: "Orientation",     icon: ClipboardCheck,  href: "/orientation",  roles: ["super_admin","academic_admin"] },
  { label: "Student Management",icon: IdCard,        href: "/student-management", roles: ["student"] },
  // Bulk Faculty/User Excel Upload task (this revision) — a bulk-created
  // account has no profile-editing UI anywhere today (PATCH /auth/me was
  // already role-agnostic on the backend but had no non-student frontend
  // consumer); "My Profile" is the minimum new page for every OTHER role to
  // self-complete DOB/Gender/Blood Group/Father's Name/Address/ABC ID/Mobile
  // later, exactly as AVFU asked. Deliberately excludes "student" — that
  // role already has its own, unchanged "Student Management" page above.
  { label: "My Profile",       icon: IdCard,         href: "/my-profile", roles: ["super_admin","academic_admin","hod","faculty","registrar","examiner","research_supervisor"] },
  { label: "PPW",              icon: FileSpreadsheet, href: "/ppw",          roles: ["student"] },
  { label: "PPW Approvals",   icon: ClipboardCheck,  href: "/ppw/approvals", roles: ["hod","faculty","research_supervisor"] },
  { label: "Users",           icon: Users,           href: "/users",        roles: ["super_admin","academic_admin"] },
  { label: "Administration",  icon: ShieldCheck,     href: "/admin",        roles: ["super_admin","academic_admin"] },
  { label: "Notifications",   icon: Bell,            href: "/notifications",roles: [] },
];

export function AMSSidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const pathname = usePathname();
  const role = useRole();
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const router = useRouter();
  const qc = useQueryClient();
  // Issue 6: self-service password change available to EVERY role, not just
  // students — surfaced here (near Logout) since it applies regardless of
  // which nav items a given role sees.
  const [showChangePw, setShowChangePw] = useState(false);

  const visible = NAV.filter((n) => n.roles.length === 0 || !role || n.roles.includes(role));

  async function logout() {
    clearAuth();
    // Cross-account stale-cache fix: logout previously only cleared the auth
    // store — the single app-wide QueryClient (created once in providers.tsx,
    // 5-minute staleTime) survives this client-side navigation, so a
    // freshly-logged-in user (even a different account, in the same browser
    // tab) could see another session's still-"fresh" cached query results
    // for up to 5 minutes (e.g. a Course Request list fetched empty just
    // before a student registered). Clearing the cache on every logout
    // guarantees the next login always starts from a clean slate.
    qc.clear();
    router.push("/login");
  }

  return (
    <aside style={{ width: collapsed ? 64 : 220 }}
      className="fixed left-0 top-0 h-full bg-white border-r border-gray-200 z-30 flex flex-col overflow-hidden transition-all duration-200">
      {/* Brand */}
      <div className="flex items-center gap-3 px-4 h-16 border-b border-gray-200 shrink-0">
        <div className="w-9 h-9 rounded-xl bg-[#0D6E6E] flex items-center justify-center shrink-0">
          <GraduationCap size={18} className="text-white" />
        </div>
        {!collapsed && (
          <div>
            <p className="text-base font-bold text-[#1A1A2E]">AVFU AMS</p>
            <p className="text-sm text-gray-700">Academic System</p>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto py-3 px-2">
        {visible.map((item) => {
          const active = pathname.startsWith(item.href);
          return (
            <a key={item.href} href={item.href} title={collapsed ? item.label : undefined}
              className={cn("group flex items-center gap-3 px-3 py-2.5 rounded-xl mb-0.5 transition-all relative",
                active ? "bg-[#E6F4F4] text-[#0D6E6E] font-semibold" : "text-gray-700 hover:bg-gray-50 hover:text-[#0D6E6E]")}>
              {active && <div className="absolute left-0 top-1.5 bottom-1.5 w-1 rounded-full bg-[#0D6E6E]" />}
              <item.icon size={18} className={cn("shrink-0", active ? "text-[#0D6E6E]" : "text-gray-600 group-hover:text-[#0D6E6E]")} />
              {!collapsed && <span className="text-base truncate">{item.label}</span>}
            </a>
          );
        })}
      </nav>

      {/* Bottom */}
      <div className="px-2 pb-3 shrink-0 space-y-1">
        <button onClick={() => setShowChangePw(true)} title={collapsed ? "Change Password" : undefined}
          className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-gray-700 hover:bg-gray-50 hover:text-[#0D6E6E] transition-colors">
          <KeyRound size={18} className="shrink-0" />
          {!collapsed && <span className="text-base">Change Password</span>}
        </button>
        <button onClick={logout} title={collapsed ? "Logout" : undefined}
          className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-gray-700 hover:bg-red-50 hover:text-red-600 transition-colors">
          <LogOut size={18} className="shrink-0" />
          {!collapsed && <span className="text-base">Logout</span>}
        </button>
        <button onClick={onToggle}
          className="w-full flex items-center justify-center gap-2 py-2 text-sm text-gray-600 hover:text-[#0D6E6E] transition-colors">
          {collapsed ? <ChevronRight size={15} /> : <><ChevronLeft size={15} /><span className="text-base">Collapse</span></>}
        </button>
      </div>
      {showChangePw && <ChangePasswordModal mode="self" onClose={() => setShowChangePw(false)} />}
    </aside>
  );
}
