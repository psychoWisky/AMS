import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(iso: string, style: "long" | "short" | "relative" = "long"): string {
  const d = new Date(iso);
  const opts: Intl.DateTimeFormatOptions = { timeZone: "Asia/Kolkata" };
  if (style === "relative") {
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return Math.floor(hrs / 24) + "d ago";
  }
  if (style === "short") {
    return d.toLocaleDateString("en-IN", { ...opts, day: "2-digit", month: "short", year: "numeric" });
  }
  return d.toLocaleDateString("en-IN", { ...opts, day: "2-digit", month: "long", year: "numeric" });
}

// Role-cleanup task: academic_admin/registrar/examiner/research_supervisor
// were dummy/testing roles, never real AVFU roles, and have been removed
// entirely (backend UserRole enum, ams_user_role Postgres enum, and here).
// Incharge Academic Cell / DPGS task (this revision) — two new GLOBAL roles
// added above HOD (SUPER_ADMIN > DPGS > INCHARGE_ACADEMIC_CELL > HOD >
// FACULTY > STUDENT), matching the backend `UserRole` enum's actual values
// exactly (never guessed).
export const ROLES = {
  super_admin: "Super Admin",
  vice_chancellor: "Vice Chancellor",
  dpgs: "DPGS",
  incharge_academic_cell: "Incharge Academic Cell",
  hod: "Head of Department",
  faculty: "Faculty",
  student: "Student",
  // External Examiner Selection task — a display label only. This role is never
  // manually assignable via User Management (see users/page.tsx's ROLE_OPTIONS);
  // an account is created automatically on VC selection.
  external_examiner: "External Examiner",
  // Initial Thesis Management task — multi-holder, departmentless, like Faculty.
  // Business-document label is "Chief Librarian" (thesis.py's signature table);
  // the assignable role name here stays "Librarian" for consistency with every
  // other role label in this map (a role name, not a document-signature title).
  librarian: "Librarian",
  // Student Migration task — single-holder, departmentless, like DPGS/Incharge/VC
  // (there is confirmed to be exactly one Registrar). Scoped only to the Migration
  // module's own endpoints — never added to a generic student-management tuple.
  registrar: "Registrar",
  // Gradesheet/Result task — single-holder, departmentless. Final approver of every
  // course-wise Gradesheet (after DPGS) and the only role that compiles/publishes
  // student-wise semester results (Pass / Pass with Backlogs).
  controller_of_examination: "Controller of Examination",
};

export const ADMIN_ROLES = ["super_admin", "hod"];

// Incharge Academic Cell / DPGS task — the two new global roles, for pages
// that need to grant them the same cross-department view access Super Admin
// already has (never user-management/role-assignment powers — those remain
// SUPER_ADMIN-only, matched by the backend exactly).
export const GLOBAL_ROLES = ["super_admin", "vice_chancellor", "incharge_academic_cell", "dpgs"];

// Centralized labels for CommitteeMember.role (BUSINESS_LOGIC.md M.5, Rule 29 —
// the 5 confirmed PG/PhD Research Committee member types, plus `co_major_advisor`,
// now confirmed OPTIONAL and selectable — Advisory Committee department-eligibility
// task, this revision). `member` is a legacy value from before the original
// confirmation — never written by new code, kept mapped here so any pre-existing
// row still displays a label instead of a raw slug.
export const COMMITTEE_ROLE_LABELS: Record<string, string> = {
  major_advisor: "Major Advisor",
  member_major: "Member Major",
  member_minor: "Member Minor",
  supporting: "Supporting",
  member_of_others: "Member of Others",
  co_major_advisor: "Co-Major Advisor",
  member: "Member",
};

// The non-Major-Advisor types selectable when a Major Advisor adds a committee
// member (BUSINESS_LOGIC.md M.5) — mirrors research.py's _MEMBER_ROLES exactly.
// Roles requiring an internal AVFU faculty member (department-eligibility
// enforced server-side) vs. the one external role (no faculty account at all).
export const COMMITTEE_MEMBER_ROLES = ["member_major", "member_minor", "supporting", "co_major_advisor", "member_of_others"] as const;
export const COMMITTEE_EXTERNAL_ROLES = ["member_of_others"] as const;
// Roles whose eligible-faculty list is scoped to the student's OWN department
// (server-authoritative — this is only used to drive the dropdown's default
// department filter, never to decide eligibility itself).
export const COMMITTEE_SAME_DEPARTMENT_ROLES = ["member_major"] as const;
export const COMMITTEE_OTHER_DEPARTMENT_ROLES = ["member_minor"] as const;

export function committeeRoleLabel(role: string): string {
  return COMMITTEE_ROLE_LABELS[role] ?? role.replace(/_/g, " ");
}

// PG -> Masters display-label task (this revision) — CONFIRMED a UI-only
// rename: the backend/database value remains the literal string "PG"
// everywhere (API requests/responses, Program.level, Course.program_level,
// ComprehensiveExamApplication/ExternalExaminerSelection.degree_level, every
// business-rule dict and eligibility gate) — never touched by this helper.
// Every frontend spot that renders a programme/course/student "level" value
// as visible text (filters, dropdown option labels, table cells, badges,
// summaries, detail views) should route through `levelLabel()` rather than
// rendering the raw string, so "Masters" is shown consistently without any
// stored value ever changing. A `<select>`/button's VALUE must still be the
// raw level ("UG"/"PG"/"PhD") — only the visible label changes.
export const LEVEL_LABELS: Record<string, string> = { UG: "UG", PG: "Masters", PhD: "PhD" };
export function levelLabel(level: string | null | undefined): string {
  if (!level) return "";
  return LEVEL_LABELS[level] ?? level;
}

// Research Course Assignment Strategy task — CourseOffering.research_assignment_type
// values (never exposed as raw enum names to users — see Section 28's explicit
// "Major Advisor" / "External Examiner" labels, not "major_advisor" / "external_examiner").
export const RESEARCH_ASSIGNMENT_TYPE_LABELS: Record<string, string> = {
  major_advisor: "Major Advisor",
  external_examiner: "External Examiner",
};
export function researchAssignmentTypeLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return RESEARCH_ASSIGNMENT_TYPE_LABELS[value] ?? value;
}

// Confirmed HOD Course Management "Course Type" values (BUSINESS_LOGIC.md L.2,
// Rule 21) — deliberately a different concept from the pre-existing
// Course.course_type (theory/practical/both), stored on the new Course.category
// field (backend/app/models/course.py).
export const COURSE_CATEGORY_LABELS: Record<string, string> = {
  optional: "Optional Course",
  core: "Core Course",
  compulsory: "Compulsory Course (CC)",
  research: "Research Course",
  seminar: "Seminar Course",
  deficiency: "Deficiency",
  bridge: "Bridge",
  prerequisite: "Prerequisite",
  mandatory_mba: "Mandatory Course (MBA)",
};

// Confirmed "Credit Type" values (BUSINESS_LOGIC.md L.2, Rule 22).
export const CREDIT_TYPE_LABELS: Record<string, string> = {
  credit: "Credit",
  non_credit: "Non-credit",
};
