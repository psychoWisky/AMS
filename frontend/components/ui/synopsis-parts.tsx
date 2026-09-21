"use client";
import { useEffect, useState } from "react";
import { X, Loader2, Download, RotateCcw } from "lucide-react";
import { api } from "@/services/api";

// Shared building blocks for the Synopsis pages (student page + approver inbox). Everything shown here is
// returned by the backend; nothing is derived or trusted client-side — the UI is never the security boundary.

export interface SynopsisStage {
  sequence: number; stage_type: string; role_label: string; assigned_to: string | null; status: string;
  acted_by: string | null; acted_role: string | null; acted_department: string | null; acted_at: string | null;
  remark: string | null; requires_otp: boolean;
}
export interface SynopsisFileMeta {
  id: string; version: number; original_filename: string; size_bytes: number; page_count: number; uploaded_at: string | null;
}
export interface RevertInfo {
  cycle_number: number; reverted_by: string | null; role: string | null; acting_as: string | null;
  department: string | null; reverted_at: string | null; remark: string | null;
}
export interface SynopsisCycle {
  cycle_number: number; status: string; submitted_at: string | null; completed_at: string | null; reverted_at: string | null;
  revert_remark: string | null; title: string | null; file: SynopsisFileMeta | null; stages: SynopsisStage[];
}
export interface SynopsisStudent {
  student_name: string; student_roll: string | null; program_name: string | null; major_discipline: string | null;
  minor_discipline: string | null; supporting_discipline: string | null; college_name: string | null;
}
export interface SynopsisDetail {
  id: string; synopsis_type: string; status: string; status_label: string; title: string | null;
  submitted_at: string | null; approved_at: string | null; student: SynopsisStudent; file: SynopsisFileMeta | null;
  is_owner: boolean; can_edit: boolean; can_admin_edit: boolean; current_cycle_number: number | null;
  committee: SynopsisStage[]; approvals: SynopsisStage[]; revert_info: RevertInfo | null; frozen_document: boolean;
  my_pending_stage: { stage_type: string; role_label: string; requires_otp: boolean } | null; history: SynopsisCycle[];
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

async function blobErrorMessage(e: unknown, fallback: string): Promise<string> {
  const data = (e as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text());
      if (typeof parsed?.detail === "string") return parsed.detail;
    } catch { /* not JSON — keep the fallback */ }
  }
  return apiErrorMessage(e, fallback);
}

export async function downloadPdf(path: string, filename: string, params?: Record<string, string>): Promise<void> {
  const res = await api.get(path, { params, responseType: "blob" });
  const url = window.URL.createObjectURL(res.data);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

export { blobErrorMessage };

const STATUS_STYLE: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700",
  reverted: "bg-red-100 text-red-700",
  approved: "bg-green-100 text-green-700",
};

export function SynopsisStatusBadge({ status, label }: { status: string; label: string }) {
  const style = STATUS_STYLE[status] ?? "bg-amber-100 text-amber-800";
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${style}`}>{label}</span>;
}

export function StageBadge({ stage }: { stage: SynopsisStage }) {
  const { status, requires_otp: signs } = stage;
  if (status === "approved") return <span className="text-green-700 text-sm font-semibold">{signs ? "✓ Signed" : "✓ Approved"}</span>;
  if (status === "reverted") return <span className="text-red-600 text-sm font-semibold">✗ Reverted</span>;
  if (status === "cancelled") return <span className="text-gray-400 text-sm">—</span>;
  return <span className="text-amber-600 text-sm font-semibold">Pending</span>;
}

export function StudentInfoCard({ student, title }: { student: SynopsisStudent; title: string | null }) {
  const rows: [string, string | null][] = [
    ["Name of the student", student.student_name], ["Roll No", student.student_roll],
    ["Programme of Study", student.program_name], ["College", student.college_name],
    ["Major Discipline", student.major_discipline], ["Minor Discipline", student.minor_discipline],
    ["Supporting Discipline", student.supporting_discipline],
  ];
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-5">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
        {rows.map(([label, value]) => (
          <p key={label}><span className="font-semibold text-gray-700">{label}:</span> <span className="text-gray-900">{value || "—"}</span></p>
        ))}
      </div>
      <p className="text-sm mt-3"><span className="font-semibold text-gray-700">Title of the Research Problem:</span> <span className="text-gray-900 font-medium">{title || "—"}</span></p>
    </div>
  );
}

function stageWho(s: SynopsisStage): string {
  return s.acted_by ?? s.assigned_to ?? "—";
}

export function ApprovalTable({ committee, approvals }: { committee: SynopsisStage[]; approvals: SynopsisStage[] }) {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          <tr>{["SL No", "Name and Designation", "Advisory", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
        </thead>
        <tbody>
          {committee.map((s, i) => (
            <tr key={`${s.sequence}-${i}`} className="border-b border-gray-50">
              <td className="px-4 py-2.5">{i + 1}</td>
              <td className="px-4 py-2.5 font-semibold text-gray-800">{s.assigned_to ?? "—"}</td>
              <td className="px-4 py-2.5 font-semibold">{s.role_label}</td>
              <td className="px-4 py-2.5"><StageBadge stage={s} />{s.acted_at && <span className="block text-xs text-gray-500">{formatDateTime(s.acted_at)}</span>}</td>
            </tr>
          ))}
          {committee.length === 0 && <tr><td colSpan={4} className="px-4 py-4 text-gray-500 text-center">No Advisory Committee members.</td></tr>}
          {approvals.map((s) => (
            <tr key={s.sequence} className="border-b border-gray-50 last:border-0 bg-gray-50/50">
              <td className="px-4 py-2.5">—</td>
              <td className="px-4 py-2.5 text-gray-800">{s.status === "approved" || s.status === "reverted" ? stageWho(s) : "Awaiting"}</td>
              <td className="px-4 py-2.5 font-semibold">{s.role_label}{!s.requires_otp && <span className="block text-xs font-normal text-gray-500">Workflow approval (not a signatory)</span>}</td>
              <td className="px-4 py-2.5"><StageBadge stage={s} />{s.acted_at && <span className="block text-xs text-gray-500">{formatDateTime(s.acted_at)}</span>}</td>
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
      <div className="flex items-center gap-2 mb-3">
        <RotateCcw size={18} className="text-red-600" />
        <h2 className="text-lg font-bold text-red-800">Synopsis Reverted</h2>
        <span className="text-xs text-red-700 font-semibold">(submission {info.cycle_number})</span>
      </div>
      <dl className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-3">
        {rows.map(([k, v]) => (
          <div key={k}><dt className="font-semibold text-red-900">{k}</dt><dd className="text-red-950">{v}</dd></div>
        ))}
      </dl>
      <p className="font-semibold text-red-900 text-sm">Remark</p>
      <p className="text-red-950 whitespace-pre-wrap text-base" data-testid="revert-remark">{info.remark}</p>
    </div>
  );
}

export function SynopsisHistory({ history }: { history: SynopsisCycle[] }) {
  if (history.length === 0) return null;
  return (
    <div className="space-y-3">
      {[...history].reverse().map((c) => (
        <div key={c.cycle_number} className="bg-white rounded-2xl border border-gray-200 p-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mb-2">
            <p className="font-bold text-gray-900">Submission {c.cycle_number}</p>
            <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${c.status === "approved" ? "bg-green-100 text-green-700" : c.status === "reverted" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-800"}`}>
              {c.status === "active" ? "In progress" : c.status[0].toUpperCase() + c.status.slice(1)}
            </span>
            <span className="text-xs text-gray-500">Submitted {formatDateTime(c.submitted_at)}</span>
            {c.file && <span className="text-xs text-gray-500">PDF: {c.file.original_filename} ({c.file.page_count} pp.)</span>}
          </div>
          {c.status === "reverted" && c.revert_remark && (
            <p className="text-sm text-red-800 bg-red-50 rounded-lg px-3 py-2 mb-2"><span className="font-semibold">Revert remark:</span> {c.revert_remark}</p>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead><tr className="text-gray-500 text-left"><th className="py-1 pr-3">Stage</th><th className="pr-3">Approver</th><th className="pr-3">Acted as</th><th className="pr-3">Action</th><th>When</th></tr></thead>
              <tbody>
                {c.stages.map((s) => (
                  <tr key={s.sequence} className="border-t border-gray-50">
                    <td className="py-1 pr-3 font-medium">{s.role_label}</td>
                    <td className="pr-3">{s.acted_by ?? s.assigned_to ?? "—"}</td>
                    <td className="pr-3">{s.acted_role ?? "—"}{s.acted_department ? ` (${s.acted_department})` : ""}</td>
                    <td className="pr-3">{s.status === "approved" ? "Approved" : s.status === "reverted" ? "Reverted" : s.status === "cancelled" ? "Not reached" : "Pending"}{s.remark ? ` — ${s.remark}` : ""}</td>
                    <td>{formatDateTime(s.acted_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

// In-app PDF viewer: the PDF is fetched through the authorized API (never a public URL), turned into a
// blob URL and shown in an iframe.
export function PdfPreviewModal({ title, path, params, filename, onClose }: {
  title: string; path: string; params?: Record<string, string>; filename: string; onClose: () => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const paramsKey = JSON.stringify(params ?? {});

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        const res = await api.get(path, { params: JSON.parse(paramsKey), responseType: "blob" });
        objectUrl = window.URL.createObjectURL(res.data);
        if (!cancelled) setUrl(objectUrl);
      } catch (e) {
        if (!cancelled) setError(await blobErrorMessage(e, "Could not load the document."));
      }
    })();
    return () => { cancelled = true; if (objectUrl) window.URL.revokeObjectURL(objectUrl); };
  }, [path, paramsKey]);

  return (
    <div className="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-2xl w-full max-w-5xl h-[90vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-100">
          <h3 className="text-lg font-bold text-gray-900">{title}</h3>
          <div className="flex items-center gap-3">
            <button onClick={() => downloadPdf(path, filename, JSON.parse(paramsKey)).catch(async (e) => setError(await blobErrorMessage(e, "Download failed.")))}
              className="flex items-center gap-1.5 px-3 py-1.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-lg text-sm font-semibold hover:bg-[#E6F4F4]">
              <Download size={14} /> Download PDF
            </button>
            <button onClick={onClose} aria-label="Close preview" className="text-gray-400 hover:text-gray-700"><X size={22} /></button>
          </div>
        </div>
        <div className="flex-1 bg-gray-100 rounded-b-2xl overflow-hidden">
          {error ? <p className="p-6 text-red-700">{error}</p>
            : !url ? <div className="h-full flex items-center justify-center"><Loader2 className="animate-spin text-gray-500" /></div>
            : <iframe src={url} title={title} className="w-full h-full" />}
        </div>
      </div>
    </div>
  );
}
