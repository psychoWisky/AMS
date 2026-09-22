import {
  LayoutDashboard, CalendarDays, BookOpen, Users, ClipboardList,
  BarChart3, FlaskConical, Bell, GraduationCap, FileText, ClipboardCheck, IdCard, UserCog, ShieldCheck, FileSpreadsheet, ScrollText, UserSearch, LayoutGrid,
  type LucideIcon,
} from "lucide-react";

export const DASHBOARD_ROUTE = "/dashboard";

export interface NavItem { label: string; icon: LucideIcon; href: string; roles: string[]; }

// Single source of truth for which ACTIVE role may open which page: the
// sidebar renders its links from this list and the (app) layout's route guard
// enforces it (see `canAccessRoute`). An empty `roles` array means any
// authenticated role. This is UX protection only — every backend endpoint
// still authorizes independently.
export const NAV: NavItem[] = [
  { label: "Dashboard",       icon: LayoutDashboard, href: "/dashboard",    roles: [] },
  { label: "Academic Calendar",icon: CalendarDays,   href: "/calendar",     roles: ["super_admin","hod","faculty","student"] },
  // "faculty" deliberately excluded here (BUSINESS_LOGIC.md Section N — Faculty
  // has no need for a generic all-courses catalog; their course-relevant view
  // is "Teacher Courses" (their own assigned offerings) below.
  // "student" also excluded (Student/Faculty module-cleanup task, this
  // revision) — the generic Course Catalogue is not a confirmed student
  // module (absent from BUSINESS_LOGIC.md K.7); students see eligible
  // offered courses via Course Registration and their own history via My
  // Courses. The `GET /courses`/`GET /courses/offerings/all` backend
  // endpoints and this route are UNCHANGED — Super Admin/HOD still need
  // them for Course Management exactly as before.
  { label: "Courses",         icon: BookOpen,        href: "/courses",      roles: ["super_admin","hod"] },
  // "hod" deliberately excluded here (BUSINESS_LOGIC.md Section N.1) — HOD's
  // course-offering management already lives at Courses -> Offer Course;
  // this page is instructor-assigned "my courses" and does not apply to HOD.
  { label: "Teacher Courses", icon: BookOpen,        href: "/teacher-courses", roles: ["super_admin","faculty"] },
  { label: "Faculties",       icon: UserCog,         href: "/faculties",    roles: ["hod"] },
  // "student" deliberately excluded here — Course Registration is now the
  // single student-facing course selection/enrollment workflow; this page
  // remains staff-only "Enrollment Management" (review/approve per-offering
  // requests) for every other role.
  // "faculty" also excluded (Student/Faculty module-cleanup task, this
  // revision) — this legacy, non-registration-stage-aware view is fully
  // superseded for Faculty by "Course Request" below (the confirmed module
  // per BUSINESS_LOGIC.md M.9, already correctly scoped to the faculty's own
  // assigned offerings). Kept for Super Admin/HOD, who may still
  // legitimately use it as a cross-department administrative tool — the
  // route/page/backend are unchanged.
  { label: "Enrollment",      icon: ClipboardList,   href: "/enrollment",   roles: ["super_admin","hod"] },
  // My Courses task (this revision) — same route (`/enrollment`), a
  // separate nav entry so students see the confirmed "My Courses" label
  // (BUSINESS_LOGIC.md K.7) instead of the staff-facing "Enrollment" label;
  // the route itself branches on role internally, unchanged.
  { label: "My Courses",      icon: BookOpen,        href: "/enrollment",   roles: ["student"] },
  { label: "Course Registration",icon: ClipboardCheck,href: "/course-registration", roles: ["student"] },
  // Incharge Academic Cell / DPGS task (this revision) — both are global
  // roles that approve Course Registration Cards after HOD (Section 12);
  // this page hosts their approval queue exactly like it already does for
  // HOD/Faculty.
  { label: "Course Request",  icon: ClipboardList,   href: "/course-request", roles: ["super_admin","hod","faculty","incharge_academic_cell","dpgs"] },
  { label: "Academic Progress",icon: GraduationCap,  href: "/academic-progress", roles: ["super_admin","hod","faculty","student","incharge_academic_cell","dpgs"] },
  { label: "Grading",         icon: BarChart3,       href: "/grading",      roles: ["super_admin","hod","faculty"] },
  { label: "Admit Card",      icon: FileText,        href: "/admit-card",   roles: ["super_admin","hod","student"] },
  // Advisory Committee naming task (this revision) — visible label only;
  // the route (/research), page component, and API endpoints are unchanged.
  // Incharge Academic Cell / DPGS task — both approve committees after HOD
  // (Section 23/25/26), global (no department restriction).
  { label: "Advisory Committee", icon: FlaskConical,  href: "/research",     roles: ["super_admin","hod","faculty","student","incharge_academic_cell","dpgs"] },
  { label: "Admissions",      icon: ClipboardCheck,  href: "/admissions",   roles: ["super_admin"] },
  { label: "Orientation",     icon: ClipboardCheck,  href: "/orientation",  roles: ["super_admin"] },
  { label: "Student Management",icon: IdCard,        href: "/student-management", roles: ["student"] },
  // Bulk Faculty/User Excel Upload task (this revision) — a bulk-created
  // account has no profile-editing UI anywhere today (PATCH /auth/me was
  // already role-agnostic on the backend but had no non-student frontend
  // consumer); "My Profile" is the minimum new page for every OTHER role to
  // self-complete DOB/Gender/Blood Group/Father's Name/Address/ABC ID/Mobile
  // later, exactly as AVFU asked. Deliberately excludes "student" — that
  // role already has its own, unchanged "Student Management" page above.
  { label: "My Profile",       icon: IdCard,         href: "/my-profile", roles: ["super_admin","hod","faculty","incharge_academic_cell","dpgs"] },
  { label: "PPW",              icon: FileSpreadsheet, href: "/ppw",          roles: ["student"] },
  // Incharge Academic Cell / DPGS task — both approve PPWs after HOD
  // (Section 19/20), global (no department restriction).
  { label: "PPW Approvals",   icon: ClipboardCheck,  href: "/ppw/approvals", roles: ["hod","faculty","incharge_academic_cell","dpgs"] },
  // Synopsis (First Synopsis) — the student's own page, and the approver inbox for every role that appears in the
  // approval chain. Both are UX gates only; each backend endpoint authorizes independently from the active session.
  { label: "Synopsis",         icon: ScrollText,      href: "/synopsis",     roles: ["student"] },
  // External Examiner Selection — the Major Advisor's own proposal page (never a student route:
  // the student has zero access to this module, by confirmed requirement), the shared approver
  // inbox, and the Vice Chancellor's own read-only institutional dashboard.
  { label: "External Examiners", icon: UserSearch,    href: "/external-examiners", roles: ["faculty"] },
  { label: "Examiner Approvals", icon: ClipboardCheck, href: "/external-examiners/approvals", roles: ["hod","faculty","incharge_academic_cell","dpgs","vice_chancellor"] },
  { label: "VC Dashboard",     icon: LayoutGrid,      href: "/vc-dashboard", roles: ["vice_chancellor"] },
  { label: "Synopsis Approvals", icon: ClipboardCheck, href: "/synopsis/approvals", roles: ["hod","faculty","incharge_academic_cell","dpgs"] },
  { label: "Users",           icon: Users,           href: "/users",        roles: ["super_admin"] },
  // Super Admin's global student management (all students, filters, full profile edit).
  // Distinct from "/student-management", which is a student's OWN profile page.
  { label: "Students",        icon: GraduationCap,   href: "/students",     roles: ["super_admin"] },
  { label: "Administration",  icon: ShieldCheck,     href: "/admin",        roles: ["super_admin"] },
  { label: "Notifications",   icon: Bell,            href: "/notifications",roles: [] },
];

// Which roles may open `pathname`, from the most specific matching NAV entry
// (so "/ppw/approvals" is not governed by "/ppw"). Several entries may share
// one href (e.g. "Enrollment"/"My Courses") — their roles are combined. A
// path matching no entry is left to Next's own 404 handling.
export function canAccessRoute(pathname: string, activeRole: string | null | undefined): boolean {
  let bestHref = "";
  for (const item of NAV) {
    if ((pathname === item.href || pathname.startsWith(item.href + "/")) && item.href.length > bestHref.length) bestHref = item.href;
  }
  if (!bestHref) return true;
  const entries = NAV.filter((n) => n.href === bestHref);
  if (entries.some((n) => n.roles.length === 0)) return true;
  return !!activeRole && entries.some((n) => n.roles.includes(activeRole));
}
