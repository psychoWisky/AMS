"use client";
// Shared building blocks for the Student Progress Report module (the student's own page +
// the shared approver inbox). Everything shown here is returned by the backend; nothing is
// derived or trusted client-side. Session Year/Session Semester are computed server-side
// from `semester_completed` and simply displayed here — never recomputed on the client.

export interface ProgressReportStage {
  sequence: number; stage_type: string; role_label: string; assigned_to: string | null; status: string;
  acted_by: string | null; acted_role: string | null; acted_department: string | null; acted_at: string | null;
  remark: string | null; requires_otp: boolean;
}
export interface RevertInfo { reverted_by: string | null; role: string | null; department: string | null; reverted_at: string | null; remark: string | null }
export interface ProceedingsInfo { id: string; version: number; original_filename: string; uploaded_at: string | null }
export interface ProgressReportDetail {
  id: string; status: string; status_label: string;
  student: { name: string; roll_no: string | null; program_name: string | null };
  academic_year_id: string; academic_year: string | null;
  semester_id: string; semester_name: string | null;
  period_from: string | null; period_to: string | null;
  semester_completed: number | null; session_year: string | null; session_semester: string | null;
  total_courses: number | null; total_credits_programme: number | null;
  current_semester_courses: number | null; current_semester_credits: number | null;
  courses_completed_till_date: number | null; credits_completed_till_date: number | null;
  research_title: string | null; research_progress: string | null;
  leave_availed: string | null; fellowship_stipend: string | null;
  // Student-entered (CORRECTED — these were mistakenly Major-Advisor-only before; AVFU
  // clarified they belong to the student). Wire value is the closed "Yes"/"No" vocabulary,
  // never a raw boolean or free text.
  expected_completion: "Yes" | "No" | null; completion_delay_reason: string | null;
  is_owner: boolean; can_edit: boolean;
  revert_info: RevertInfo | null;
  current_cycle_number: number | null;
  stages: ProgressReportStage[]; history: (ProgressReportStage & { cycle_number: number })[];
  my_pending_stage: { stage_type: string; role_label: string; requires_otp: boolean } | null;
  proceedings?: ProceedingsInfo | null;
  // Major Advisor's own fields (CORRECTED — the actual Major Advisor fields). Genuinely ABSENT
  // from the response for a Student viewer (confidentiality-by-omission), present read-only for
  // every other authorized viewer; `can_edit_advisor_fields` is true only for the current live
  // Major Advisor while the report is at their stage.
  advisory_remark?: string | null; overall_progress?: string | null; student_conduct?: string | null;
  can_edit_advisor_fields?: boolean;
  submitted_at: string | null; approved_at: string | null;
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

export function apiErrorMessage(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}

const STATUS_STYLE: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700",
  reverted: "bg-red-100 text-red-700",
  approved: "bg-green-100 text-green-700",
};

export function ProgressReportStatusBadge({ status, label }: { status: string; label: string }) {
  const style = STATUS_STYLE[status] ?? "bg-amber-100 text-amber-800";
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${style}`}>{label}</span>;
}

export function StageBadge({ stage }: { stage: ProgressReportStage }) {
  const { status, requires_otp: signs } = stage;
  if (status === "approved") return <span className="text-green-700 text-sm font-semibold">{signs ? "✓ Signed" : "✓ Approved"}</span>;
  if (status === "reverted") return <span className="text-red-600 text-sm font-semibold">✗ Reverted</span>;
  if (status === "cancelled") return <span className="text-gray-400 text-sm">—</span>;
  return <span className="text-amber-600 text-sm font-semibold">Pending</span>;
}

export function StudentInfoCard({ r }: { r: ProgressReportDetail }) {
  const rows: [string, string | null][] = [
    ["Roll No", r.student.roll_no], ["Name", r.student.name], ["Programme of Study", r.student.program_name],
  ];
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
      {rows.map(([label, value]) => (
        <p key={label}><span className="font-semibold text-gray-700">{label}:</span> <span className="text-gray-900">{value || "—"}</span></p>
      ))}
    </div>
  );
}

export function StageTimeline({ stages }: { stages: ProgressReportStage[] }) {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          <tr>{["Stage", "Assigned To", "Status", "Acted By", "When"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
        </thead>
        <tbody>
          {stages.map((s) => (
            <tr key={s.sequence} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-2.5 font-semibold">{s.role_label}{!s.requires_otp && <span className="block text-xs font-normal text-gray-500">Workflow approval (not a signatory)</span>}</td>
              <td className="px-4 py-2.5 text-gray-700">{s.assigned_to ?? "—"}</td>
              <td className="px-4 py-2.5"><StageBadge stage={s} /></td>
              <td className="px-4 py-2.5 text-gray-700">{s.status === "approved" || s.status === "reverted" ? s.acted_by : "—"}</td>
              <td className="px-4 py-2.5 text-gray-500">{formatDateTime(s.acted_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ApprovalHistoryTable({ history }: { history: (ProgressReportStage & { cycle_number: number })[] }) {
  const acted = history.filter((h) => h.status === "approved" || h.status === "reverted");
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          <tr>{["Role / Person", "Action", "Date", "Remark"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
        </thead>
        <tbody>
          {acted.map((h, i) => (
            <tr key={i} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-2.5 font-semibold text-gray-800">{h.acted_by ?? h.role_label}</td>
              <td className="px-4 py-2.5">{h.status === "approved" ? <span className="text-green-700 font-semibold">Approved</span> : <span className="text-red-600 font-semibold">Reverted</span>}</td>
              <td className="px-4 py-2.5 text-gray-500">{formatDateTime(h.acted_at)}</td>
              <td className="px-4 py-2.5 text-gray-700">{h.remark || "—"}</td>
            </tr>
          ))}
          {acted.length === 0 && <tr><td colSpan={4} className="px-4 py-4 text-gray-500 text-center">No approval action recorded yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function RevertNotice({ info }: { info: RevertInfo }) {
  const rows: [string, string][] = [
    ["Reverted By", info.reverted_by ?? "—"],
    ["Role", `${info.role ?? "—"}${info.department ? ` (${info.department})` : ""}`],
    ["Date", formatDateTime(info.reverted_at)],
  ];
  return (
    <div className="rounded-2xl border-2 border-red-300 bg-red-50 p-5" role="alert">
      <h2 className="text-lg font-bold text-red-800 mb-3">Reverted for Correction</h2>
      <dl className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-3">
        {rows.map(([k, v]) => (
          <div key={k}><dt className="font-semibold text-red-900">{k}</dt><dd className="text-red-950">{v}</dd></div>
        ))}
      </dl>
      <p className="font-semibold text-red-900 text-sm">Remark</p>
      <p className="text-red-950 whitespace-pre-wrap text-base" data-testid="progress-report-revert-remark">{info.remark}</p>
    </div>
  );
}

// Read-only display of the Major Advisor's own three fields — used by every approver AFTER
// the Major Advisor (committee members, HOD, Incharge Academic Cell, DPGS). Never rendered on
// the student's own page at all — the backend never even sends these keys to a student viewer.
export function MajorAdvisorAssessment({ r }: { r: ProgressReportDetail }) {
  const rows: [string, string | null | undefined][] = [
    ["Advisory Remark", r.advisory_remark], ["Overall Progress of the Student", r.overall_progress], ["Student Conduct", r.student_conduct],
  ];
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-3">
      <p className="text-sm font-semibold text-gray-700">Major Advisor Assessment</p>
      {rows.map(([label, value]) => (
        <div key={label}>
          <p className="text-xs font-semibold text-gray-500">{label}:</p>
          <p className="text-sm text-gray-800 whitespace-pre-wrap">{value || "—"}</p>
        </div>
      ))}
    </div>
  );
}
