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

export const ROLES = {
  super_admin: "Super Admin",
  academic_admin: "Academic Admin",
  hod: "Head of Department",
  faculty: "Faculty",
  student: "Student",
  registrar: "Registrar",
  examiner: "Examiner",
  research_supervisor: "Research Supervisor",
};

export const ADMIN_ROLES = ["super_admin", "academic_admin", "registrar", "examiner", "hod"];

// Centralized labels for CommitteeMember.role (BUSINESS_LOGIC.md M.5, Rule 29 —
// the 5 confirmed PG/PhD Research Committee member types). `co_major_advisor` and
// `member` are legacy values from before this confirmation — never written by new
// code (research.py's _MEMBER_ROLES no longer includes them), but kept mapped here
// so any pre-existing rows still display a label instead of a raw slug.
export const COMMITTEE_ROLE_LABELS: Record<string, string> = {
  major_advisor: "Major Advisor",
  member_major: "Member Major",
  member_minor: "Member Minor",
  supporting: "Supporting",
  member_of_others: "Member of Others",
  co_major_advisor: "Co-Major Advisor",
  member: "Member",
};

// The 4 non-Major-Advisor confirmed types selectable when a Major Advisor adds a
// committee member (BUSINESS_LOGIC.md M.5) — mirrors research.py's _MEMBER_ROLES.
export const COMMITTEE_MEMBER_ROLES = ["member_major", "member_minor", "supporting", "member_of_others"] as const;

export function committeeRoleLabel(role: string): string {
  return COMMITTEE_ROLE_LABELS[role] ?? role.replace(/_/g, " ");
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
