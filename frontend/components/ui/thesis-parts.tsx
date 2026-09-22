"use client";
// Shared building blocks for Initial Thesis Management (the student's own page + the shared
// approver inbox). Everything shown here is returned by the backend; nothing is derived or
// trusted client-side. Final Thesis is out of scope — nothing here assumes it exists.

export interface ThesisDocumentInfo {
  id: string; version: number; original_filename: string; size_bytes: number;
  uploaded_at: string | null; uploaded_by: string | null;
}
export interface ThesisStage {
  sequence: number; stage_type: string; role_label: string; assigned_to: string | null; status: string;
  acted_by: string | null; acted_role: string | null; acted_department: string | null; acted_at: string | null;
  remark: string | null; requires_otp: boolean;
}
export interface SignatureRow { role_label: string; status: "Signed" | "Pending"; acted_at: string | null }
export interface RevertInfo { reverted_by: string | null; role: string | null; department: string | null; reverted_at: string | null; remark: string | null }
export interface ExternalReportRow {
  id: string; name: string; status: string; report_available: boolean; submitted_at: string | null; dpgs_approved_at: string | null;
}
export interface ThesisDetail {
  id: string; thesis_type: string; status: string; status_label: string; title: string | null;
  student: { name: string; roll_no: string | null; degree_name: string | null; department_name: string | null; college_name: string | null };
  plagiarism_student_percent: number | null; plagiarism_software_name: string | null; plagiarism_library_percent: number | null;
  abstract: string | null;
  documents: Record<string, ThesisDocumentInfo | null>;
  is_owner: boolean; can_edit: boolean;
  revert_info: RevertInfo | null;
  my_pending_stage: { stage_type: string; role_label: string; requires_otp: boolean } | null;
  my_evaluation: ExternalReportRow | null;
  stages?: ThesisStage[]; signature_table?: SignatureRow[];
  external_report?: ExternalReportRow[]; external_evaluation_completed?: boolean;
}

export const STUDENT_DOCUMENT_LABELS: Record<string, string> = {
  thesis_file: "Thesis File",
  plagiarism_student_report: "Plagiarism Report (Student)",
  payment_receipt: "Payment Receipt",
  seminar_proceedings: "Proceedings of the Thesis Seminar",
  clearance: "Clearance",
  declaration_annexure1: "Student Declaration (Annexure-I)",
  seminar_certificate_pg25: "Thesis Seminar Certificate (Form No. PG 25)",
  certificate_i_pg27: "Certificate I (Form No. PG 27)",
  annexure_iv: "Annexure-IV",
};
// The last four support "print"/view framing in the UI; the first four are plain upload/view.
export const PRINTABLE_DOCUMENT_TYPES = new Set(["declaration_annexure1", "seminar_certificate_pg25", "certificate_i_pg27", "annexure_iv"]);

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

export function ThesisStatusBadge({ status, label }: { status: string; label: string }) {
  const style = STATUS_STYLE[status] ?? "bg-amber-100 text-amber-800";
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${style}`}>{label}</span>;
}

export function StageBadge({ stage }: { stage: ThesisStage }) {
  const { status, requires_otp: signs } = stage;
  if (status === "approved") return <span className="text-green-700 text-sm font-semibold">{signs ? "✓ Signed" : "✓ Approved"}</span>;
  if (status === "reverted") return <span className="text-red-600 text-sm font-semibold">✗ Reverted</span>;
  if (status === "cancelled") return <span className="text-gray-400 text-sm">—</span>;
  return <span className="text-amber-600 text-sm font-semibold">Pending</span>;
}

export function StudentInfoCard({ student }: { student: ThesisDetail["student"] }) {
  const rows: [string, string | null][] = [
    ["Name", student.name], ["Roll No", student.roll_no],
    ["Degree", student.degree_name], ["Department", student.department_name], ["College", student.college_name],
  ];
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
      {rows.map(([label, value]) => (
        <p key={label}><span className="font-semibold text-gray-700">{label}:</span> <span className="text-gray-900">{value || "—"}</span></p>
      ))}
    </div>
  );
}

export function StageTimeline({ stages }: { stages: ThesisStage[] }) {
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

export function SignatureTable({ rows }: { rows: SignatureRow[] }) {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          <tr>{["Signature", "Date"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.role_label} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-2.5 font-semibold text-gray-800">{r.role_label}</td>
              <td className="px-4 py-2.5">
                <span className={r.status === "Signed" ? "text-green-700 font-semibold" : "text-amber-600 font-semibold"}>{r.status}</span>
                {r.acted_at && <span className="ml-2 text-gray-500">{formatDateTime(r.acted_at)}</span>}
              </td>
            </tr>
          ))}
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
      <h2 className="text-lg font-bold text-red-800 mb-3">Reverted to You for Correction</h2>
      <dl className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-3">
        {rows.map(([k, v]) => (
          <div key={k}><dt className="font-semibold text-red-900">{k}</dt><dd className="text-red-950">{v}</dd></div>
        ))}
      </dl>
      <p className="font-semibold text-red-900 text-sm">Remark</p>
      <p className="text-red-950 whitespace-pre-wrap text-base" data-testid="thesis-revert-remark">{info.remark}</p>
    </div>
  );
}

export function ExternalReportTable({ rows }: { rows: ExternalReportRow[] }) {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          <tr>{["#SL NO", "Name", "Evaluated Thesis", "Evaluation Report", "Status"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-2.5">{i + 1}</td>
              <td className="px-4 py-2.5 font-semibold text-gray-800">{r.name}</td>
              <td className="px-4 py-2.5 text-gray-500">N/A</td>
              <td className="px-4 py-2.5">{r.report_available ? "Report" : "—"}</td>
              <td className="px-4 py-2.5"><span className="text-green-700 font-semibold">{r.status === "approved" ? "Signed" : r.status}</span></td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="px-4 py-4 text-gray-500 text-center">No approved external report yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
