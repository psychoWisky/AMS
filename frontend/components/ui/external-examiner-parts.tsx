"use client";
// Shared building blocks for the External Examiner Selection pages (Major Advisor propose page +
// the shared approver inbox). Everything shown here is returned by the backend; nothing is derived
// or trusted client-side, and there is deliberately NO student-facing variant of any of this.

export interface ExaminerProposal {
  id: string; slot_number: number; name: string; specialization: string; designation: string;
  email: string; phone: string; institution: string; is_reused_examiner: boolean; edited: boolean;
}
export interface ExaminerStage {
  sequence: number; stage_type: string; role_label: string; assigned_to: string | null; status: string;
  acted_by: string | null; acted_role: string | null; acted_department: string | null; acted_at: string | null;
  remark: string | null; requires_otp: boolean;
}
export interface RevertInfo {
  cycle_number: number; reverted_by: string | null; role: string | null; acting_as: string | null;
  department: string | null; reverted_at: string | null; remark: string | null;
}
export interface ExaminerCycle {
  cycle_number: number; status: string; submitted_at: string | null; completed_at: string | null;
  reverted_at: string | null; revert_remark: string | null; proposals: ExaminerProposal[]; stages: ExaminerStage[];
}
export interface SelectionDetail {
  id: string; status: string; status_label: string; degree_level: "PG" | "PhD";
  required_proposal_count: number; required_selection_count: number;
  student: { name: string; roll_no: string | null; program_name: string | null; department_name: string | null; college_name: string | null };
  is_major_advisor: boolean; can_edit: boolean; current_cycle_number: number | null;
  proposals: ExaminerProposal[]; stages: ExaminerStage[]; revert_info: RevertInfo | null;
  selection_completed: boolean; selected_proposal_ids?: string[];
  my_pending_stage: { stage_type: string; role_label: string; requires_otp: boolean } | null;
  history: ExaminerCycle[];
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

export function SelectionStatusBadge({ status, label }: { status: string; label: string }) {
  const style = STATUS_STYLE[status] ?? "bg-amber-100 text-amber-800";
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${style}`}>{label}</span>;
}

export function StageBadge({ stage }: { stage: ExaminerStage }) {
  const { status, requires_otp: signs } = stage;
  if (status === "approved") return <span className="text-green-700 text-sm font-semibold">{signs ? "✓ Signed" : "✓ Approved"}</span>;
  if (status === "reverted") return <span className="text-red-600 text-sm font-semibold">✗ Reverted</span>;
  if (status === "cancelled") return <span className="text-gray-400 text-sm">—</span>;
  return <span className="text-amber-600 text-sm font-semibold">Pending</span>;
}

export function StudentInfoCard({ student, degreeLevel }: { student: SelectionDetail["student"]; degreeLevel: string }) {
  const rows: [string, string | null][] = [
    ["Name of the student", student.name], ["Roll No", student.roll_no],
    ["Programme", student.program_name], ["Degree Level", degreeLevel],
    ["Department", student.department_name], ["College", student.college_name],
  ];
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
      {rows.map(([label, value]) => (
        <p key={label}><span className="font-semibold text-gray-700">{label}:</span> <span className="text-gray-900">{value || "—"}</span></p>
      ))}
    </div>
  );
}

export function ProposalTable({ proposals, selectable, selected, onToggle, maxSelectable }: {
  proposals: ExaminerProposal[]; selectable?: boolean; selected?: Set<string>;
  onToggle?: (id: string) => void; maxSelectable?: number;
}) {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          <tr>
            {selectable && <th className="px-4 py-2.5 w-10"></th>}
            {["Slot", "Name", "Specialization", "Designation", "Email", "Phone", "Institution"].map((h) => (
              <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {proposals.map((p) => {
            const isSelected = selected?.has(p.id) ?? false;
            const disableNew = selectable && !isSelected && selected && maxSelectable !== undefined && selected.size >= maxSelectable;
            return (
              <tr key={p.id} className={`border-b border-gray-50 last:border-0 ${isSelected ? "bg-[#E6F4F4]" : ""}`}>
                {selectable && (
                  <td className="px-4 py-2.5">
                    <input type="checkbox" checked={isSelected} disabled={!!disableNew} onChange={() => onToggle?.(p.id)}
                      className="w-4 h-4 accent-[#0D6E6E]" />
                  </td>
                )}
                <td className="px-4 py-2.5">{p.slot_number}</td>
                <td className="px-4 py-2.5 font-semibold text-gray-800">{p.name}{p.is_reused_examiner && <span className="ml-1.5 text-xs font-normal text-teal-700">(known examiner)</span>}{p.edited && <span className="ml-1.5 text-xs font-normal text-amber-700">(edited)</span>}</td>
                <td className="px-4 py-2.5">{p.specialization}</td>
                <td className="px-4 py-2.5">{p.designation}</td>
                <td className="px-4 py-2.5">{p.email}</td>
                <td className="px-4 py-2.5">{p.phone}</td>
                <td className="px-4 py-2.5">{p.institution}</td>
              </tr>
            );
          })}
          {proposals.length === 0 && <tr><td colSpan={selectable ? 8 : 7} className="px-4 py-4 text-gray-500 text-center">No proposals.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function StageTimeline({ stages }: { stages: ExaminerStage[] }) {
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

export function RevertNotice({ info }: { info: RevertInfo }) {
  const rows: [string, string][] = [
    ["Reverted By", info.reverted_by ?? "—"],
    ["Role", `${info.role ?? "—"}${info.department ? ` (${info.department})` : ""}`],
    ["Date", formatDateTime(info.reverted_at)],
  ];
  return (
    <div className="rounded-2xl border-2 border-red-300 bg-red-50 p-5" role="alert">
      <h2 className="text-lg font-bold text-red-800 mb-3">Reverted to Major Advisor</h2>
      <dl className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-3">
        {rows.map(([k, v]) => (
          <div key={k}><dt className="font-semibold text-red-900">{k}</dt><dd className="text-red-950">{v}</dd></div>
        ))}
      </dl>
      <p className="font-semibold text-red-900 text-sm">Remark</p>
      <p className="text-red-950 whitespace-pre-wrap text-base" data-testid="ee-revert-remark">{info.remark}</p>
    </div>
  );
}
