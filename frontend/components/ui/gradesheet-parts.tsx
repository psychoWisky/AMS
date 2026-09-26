"use client";
import { useEffect, useState } from "react";
import { Download, Loader2, X } from "lucide-react";
import { api, blobErrorMessage } from "@/services/api";

// Shared building blocks for the Gradesheet + Result pages. Everything shown here is returned
// by the backend; authorization is enforced server-side on every endpoint — nothing in this
// file grants access.

export interface AssignedCourse {
  offering_id: string; college_degree: string | null; department: string | null;
  course_number: string; course_title: string; course_credit: string;
  semester: string | null; semester_id: string; calendar_id: string; academic_year: string | null;
  total_students: number; gradesheet_count: number;
}
export interface OfferingInfo {
  id: string; college: string | null; degree: string | null; department: string | null;
  course_number: string; course_title: string; credit: string; credit_type: string;
  semester: string | null; session: string | null; total_students: number;
}
export interface SheetRow {
  id: string; offering_id: string; teacher: string | null; gradesheet_type: string; status: string;
  created_at: string | null; submitted_at: string | null; course_number: string; course_title: string;
  department: string | null; semester: string | null; academic_year: string | null;
}
export interface OfferingSheets {
  offering: OfferingInfo; can_create: boolean; sheets: SheetRow[];
  students: { id: string; name: string; roll_no: string | null }[];
}
export interface ComponentDef { code: string; name: string; component_type: "theory" | "practical"; max_marks: number; sort_order: number; }
export interface EntryRow {
  id: string; student_id: string; student_name: string | null; student_roll: string | null;
  component_marks: Record<string, number | null>;
  theory_total: number | null; practical_total: number | null; grand_total: number | null; marks_percent: number | null;
  grade_letter: string | null; grade_points: number | null; complete: boolean;
  attendance_percent: number | null; attendance_band: "red" | "yellow" | "green" | null;
  is_absent: boolean; remark: string | null;
}
export interface Signatory {
  id: string; sequence: number; stage_type: string; label: string; status: string; name: string | null;
  is_deemed: boolean; signed_at: string | null; signed_at_display: string | null; acted_at: string | null; remark: string | null;
}
export interface Signatories {
  cycle_number: number; cycle_status: string; instructors: Signatory[];
  hod: Signatory | null; incharge: Signatory | null; dpgs: Signatory | null; coe: Signatory | null;
}
export interface GradesheetDetail {
  id: string; offering_id: string; gradesheet_type: string; related_sheet_id: string | null;
  status: string; is_locked: boolean; teacher: string | null;
  created_at: string | null; submitted_at: string | null; finalized_at: string | null;
  course: { college: string | null; degree: string | null; department: string | null; course_number: string; course_title: string; credit: string; credit_type: string; semester: string | null; session: string | null };
  students: { total: number; male: number; female: number };
  structure: { total_theory_marks: number; theory_pass_marks: number; total_practical_marks: number; practical_pass_marks: number; components: ComponentDef[] };
  entries: EntryRow[];
  signatories: Signatories | null;
  instructor_deadline_at: string | null;
  permissions: { can_edit_data: boolean; can_edit_structure: boolean; can_submit: boolean; can_approve: boolean; can_revert: boolean; acting_as: string | null; awaiting: string | null };
}
export interface ApprovalCycle { id: string; cycle_number: number; status: string; submitted_at: string | null; instructor_deadline_at: string | null; closed_at: string | null; stages: Signatory[]; }

export const GRADESHEET_TYPE_LABELS: Record<string, string> = { new: "New", repeat: "Repeat", revised: "Revised", make_up: "Make up" };
export const GRADESHEET_STATUS_LABELS: Record<string, string> = {
  draft: "Draft", instructor_pending: "Awaiting Course Instructor(s)", hod_pending: "Awaiting HOD",
  incharge_pending: "Awaiting Incharge Academic Cell", dpgs_pending: "Awaiting DPGS", coe_pending: "Awaiting Controller of Examination",
  approved: "Finalized", reverted: "Reverted",
};
const STATUS_STYLE: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700", instructor_pending: "bg-amber-100 text-amber-700", hod_pending: "bg-blue-100 text-blue-700",
  incharge_pending: "bg-indigo-100 text-indigo-700", dpgs_pending: "bg-purple-100 text-purple-700", coe_pending: "bg-teal-100 text-teal-700",
  approved: "bg-green-100 text-green-700", reverted: "bg-red-100 text-red-700",
};

export function GradesheetStatusBadge({ status }: { status: string }) {
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${STATUS_STYLE[status] ?? "bg-gray-100 text-gray-700"}`}>{GRADESHEET_STATUS_LABELS[status] ?? status}</span>;
}

export function apiErrorMessage(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Confirmed attendance convention: < 75 red, 75-85 yellow, > 85 green.
const BAND_STYLE = { red: "bg-red-100 text-red-700", yellow: "bg-yellow-100 text-yellow-800", green: "bg-green-100 text-green-700" } as const;
export function attendanceBand(p: number | null | undefined): "red" | "yellow" | "green" | null {
  if (p === null || p === undefined || Number.isNaN(p)) return null;
  return p < 75 ? "red" : p <= 85 ? "yellow" : "green";
}
export function AttendanceBadge({ percent }: { percent: number | null | undefined }) {
  const band = attendanceBand(percent);
  if (percent === null || percent === undefined || !band) return <span className="text-gray-500">—</span>;
  return <span className={`inline-flex px-2 py-0.5 rounded-md text-sm font-bold ${BAND_STYLE[band]}`}>{percent.toFixed(2)}%</span>;
}

function stageText(s: Signatory): string {
  if (s.status === "approved") return `Signed ${s.signed_at_display ?? formatDateTime(s.signed_at)}`;
  if (s.status === "deemed_approved") return `Not signed — auto-forwarded after 24 hours (${s.signed_at_display ?? formatDateTime(s.acted_at)})`;
  if (s.status === "reverted") return `Reverted ${s.signed_at_display ?? formatDateTime(s.acted_at)}`;
  if (s.status === "cancelled") return "Cancelled";
  return "Pending";
}
const STAGE_TEXT_STYLE: Record<string, string> = {
  approved: "text-green-700", deemed_approved: "text-amber-700", reverted: "text-red-700", cancelled: "text-gray-500", pending: "text-gray-600",
};

// Signatories of the latest approval cycle — each approval IS the signature (approve = sign);
// a deemed (24 h auto-forward) approval is shown as such and never as a signature.
export function SignatoryPanel({ signatories }: { signatories: Signatories | null }) {
  if (!signatories) return <p className="text-sm text-gray-600">Not yet submitted for approval — no signatures recorded.</p>;
  const rows: { role: string; s: Signatory | null }[] = [
    ...signatories.instructors.map((s) => ({ role: s.stage_type === "creator" ? "Course Instructor (Prepared by)" : "Course Instructor", s })),
    { role: "HOD", s: signatories.hod }, { role: "Incharge Academic Cell", s: signatories.incharge },
    { role: "DPGS", s: signatories.dpgs }, { role: "Controller of Examination", s: signatories.coe },
  ];
  return (
    <div className="overflow-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200"><tr>
          {["Role", "Name", "Signature"].map((h) => <th key={h} className="text-left px-3 py-2 font-semibold text-gray-700">{h}</th>)}
        </tr></thead>
        <tbody>
          {rows.map(({ role, s }, i) => (
            <tr key={`${role}-${i}`} className="border-b border-gray-100">
              <td className="px-3 py-2 font-medium">{role}</td>
              <td className="px-3 py-2">{s?.name ?? "—"}</td>
              <td className={`px-3 py-2 font-semibold ${STAGE_TEXT_STYLE[s?.status ?? "pending"]}`}>
                {s ? stageText(s) : "Pending"}
                {s?.status === "reverted" && s.remark && <span className="block font-normal text-gray-700">Remark: {s.remark}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ApprovalHistory({ cycles }: { cycles: ApprovalCycle[] }) {
  if (!cycles.length) return <p className="text-sm text-gray-600">No approval cycles yet.</p>;
  return (
    <div className="space-y-4">
      {[...cycles].reverse().map((c) => (
        <div key={c.id} className="border border-gray-200 rounded-xl p-3">
          <p className="text-sm font-semibold text-gray-800 mb-2">
            Cycle {c.cycle_number} · {c.status === "completed" ? "Completed" : c.status === "reverted" ? "Reverted" : "In progress"} · submitted {formatDateTime(c.submitted_at)}
          </p>
          <ul className="text-sm space-y-1">
            {c.stages.map((s) => (
              <li key={s.id} className="flex flex-wrap gap-x-2">
                <span className="font-medium w-48">{s.label}{s.stage_type === "creator" ? " (Prepared by)" : ""}</span>
                <span className="text-gray-700">{s.name ?? "—"}</span>
                <span className={`font-semibold ${STAGE_TEXT_STYLE[s.status]}`}>— {stageText(s)}</span>
                {s.remark && s.status !== "deemed_approved" && <span className="text-gray-700">· “{s.remark}”</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

// Authenticated PDF preview: the file is fetched through the Bearer-authenticated `api` client
// as a Blob and shown from an object URL (same mechanism as `viewFileInNewTab` — never a raw
// `window.open(apiUrl)`), with Close and Download.
export function PdfPreviewModal({ title, path, filename, onClose }: { title: string; path: string; filename: string; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get(path, { responseType: "blob" });
        if (cancelled) return;
        objectUrl = window.URL.createObjectURL(res.data);
        setUrl(objectUrl);
      } catch (e) {
        if (!cancelled) setError(await blobErrorMessage(e, "Could not load the document."));
      }
    })();
    return () => { cancelled = true; if (objectUrl) window.URL.revokeObjectURL(objectUrl); };
  }, [path]);

  return (
    <div className="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-5xl h-[90vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-100">
          <h3 className="text-lg font-bold text-gray-900">{title}</h3>
          <button onClick={onClose} aria-label="Close" className="text-gray-500 hover:text-gray-800"><X size={20} /></button>
        </div>
        <div className="flex-1 bg-gray-100 min-h-0">
          {error ? <div className="h-full flex items-center justify-center text-red-600 px-6 text-center">{error}</div>
            : !url ? <div className="h-full flex items-center justify-center text-gray-600"><Loader2 className="animate-spin mr-2" />Generating document…</div>
            : <iframe src={url} title={title} className="w-full h-full border-0" />}
        </div>
        <div className="flex justify-end gap-3 px-5 py-3 border-t border-gray-100">
          <button onClick={onClose} className="px-5 py-2 border border-gray-200 rounded-xl font-semibold text-gray-700 hover:bg-gray-50">Close</button>
          {url && (
            <a href={url} download={filename} className="inline-flex items-center gap-2 px-5 py-2 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#0a5858]">
              <Download size={16} /> Download
            </a>
          )}
        </div>
      </div>
    </div>
  );
}
