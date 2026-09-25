"use client";

// Shared building blocks for the Comprehensive Examination pages (student page + approver
// inbox — one page serves both, like Advisory Committee/research/page.tsx does). Everything
// shown here is returned by the backend; nothing is derived or trusted client-side.

export interface ApplicationListItem {
  id: string; application_label: string; degree_level: string; status: string;
  student_name: string | null; student_roll: string | null;
}
export interface VivaSummary {
  id: string; attempt_number: number; viva_date: string; result: string; has_report: boolean;
}
export interface ReportSignature { faculty_id: string; role_snapshot: string; status: string; }
export interface LatestReport { id: string; version_number: number; status: string; signatures: ReportSignature[]; }
export interface ApplicationExternalPanelSummary {
  selection_status: string; cycle_id: string | null; cycle_status: string | null;
  current_stage: PanelStage | null; selected_proposal_id: string | null;
}
// uploaded -> committee_pending -> hod_pending -> incharge_pending -> dpgs_pending -> approved
// (or reverted, terminal for that version, from any pending stage). Physical signatures already
// exist inside the uploaded PDF — every stage here is a plain Approve/Revert workflow record,
// never an electronic signature.
export interface ExternalReportSignature { faculty_id: string; role_snapshot: string; status: string; signed_at: string | null; }
export interface ExternalReportDetail {
  id: string; version_number: number; status: string;
  uploaded_at: string | null; submitted_at: string | null;
  signatures: ExternalReportSignature[];
  hod_approved: boolean; hod_approved_at: string | null;
  incharge_approved: boolean; incharge_approved_at: string | null;
  dpgs_approved: boolean; dpgs_approved_at: string | null;
  approved_at: string | null; reverted_at: string | null; revert_remark: string | null;
}
export const EXTERNAL_REPORT_STAGE_LABELS: Record<string, string> = {
  uploaded: "Awaiting Major Advisor Submission",
  committee_pending: "Awaiting Advisory Committee Approval",
  hod_pending: "Awaiting HOD Approval",
  incharge_pending: "Awaiting Incharge Academic Cell Approval",
  dpgs_pending: "Awaiting DPGS Approval",
  approved: "Approved",
  reverted: "Reverted",
};
export interface ApplicationDetail {
  id: string; student_id: string; degree_level: string; status: string; application_label: string;
  major_credits_required: number; minor_credits_required: number;
  major_credits_completed: number; minor_credits_completed: number;
  ma_id: string | null; revert_remark: string | null;
  vivas: VivaSummary[]; latest_viva_report: LatestReport | null;
  external_panel: ApplicationExternalPanelSummary | null;
  external_viva_report: ExternalReportDetail | null;
}
export interface PanelProposal {
  id: string; slot_number: number; name: string; specialization: string; designation: string;
  email: string; phone: string; institution: string;
}
// "hod"/"incharge_academic_cell"/"dpgs"/"vc" while active; null once the cycle is
// approved/reverted (see backend `_panel_current_stage`) — the single source of truth for
// whose turn it is; never re-derived independently on the frontend.
export type PanelStage = "hod" | "incharge_academic_cell" | "dpgs" | "vc" | "approved";
export interface PanelDetail {
  id: string; status: string; application_id: string; degree_level: string;
  student_name: string | null; student_roll: string | null; department_name: string | null;
  current_stage: PanelStage | null;
  hod_approved: boolean; hod_approved_at: string | null;
  incharge_approved: boolean; incharge_approved_at: string | null;
  dpgs_approved: boolean; dpgs_approved_at: string | null;
  reverted_at: string | null; revert_remark: string | null;
  proposals: PanelProposal[];
  selected_proposal_id: string | null; vc_selection_completed: boolean;
  email_sent_at: string | null; required_selection_count: number;
}
export interface PanelPendingRow {
  cycle_id: string; application_id: string; student_name: string | null; student_roll: string | null;
  department_name: string | null; degree_level: string; current_stage: PanelStage; submitted_at: string | null;
}

export const PANEL_STAGE_LABELS: Record<string, string> = {
  hod: "HOD Approval", incharge_academic_cell: "Incharge Academic Cell Approval",
  dpgs: "DPGS Approval", vc: "VC Selection", approved: "External Examiner Selected",
};

export const STATUS_STYLE: Record<string, string> = {
  generated: "bg-gray-100 text-gray-700",
  ma_pending: "bg-amber-100 text-amber-700",
  hod_pending: "bg-blue-100 text-blue-700",
  incharge_pending: "bg-indigo-100 text-indigo-700",
  dpgs_pending: "bg-purple-100 text-purple-700",
  committee_pending: "bg-blue-100 text-blue-700",
  uploaded: "bg-gray-100 text-gray-700",
  draft: "bg-gray-100 text-gray-700",
  active: "bg-amber-100 text-amber-700",
  approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
  // External Panel `current_stage` values (see PANEL_STAGE_LABELS) share this same badge.
  hod: "bg-blue-100 text-blue-700",
  incharge_academic_cell: "bg-indigo-100 text-indigo-700",
  dpgs: "bg-purple-100 text-purple-700",
  vc: "bg-amber-100 text-amber-700",
};

export function ComprehensiveExamStatusBadge({ status, label: labelOverride }: { status: string; label?: string }) {
  const style = STATUS_STYLE[status] ?? "bg-amber-100 text-amber-800";
  const label = labelOverride ?? status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${style}`}>{label}</span>;
}

export function apiErrorMessage(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
