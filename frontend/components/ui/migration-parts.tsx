"use client";
// Shared building blocks for Student Migration (the student's own page + the Registrar's
// approval inbox). Everything shown here is returned by the backend; nothing is derived or
// trusted client-side. There is no multi-stage chain here (Student -> Registrar only), so —
// unlike Thesis/Synopsis/External Examiner Selection — there is no stage timeline or
// signature table component; only a single decision block.

export interface ReceiptInfo { original_filename: string | null; size_bytes: number | null; uploaded_at: string | null }
export interface MigrationDetail {
  id: string; status: string; status_label: string;
  student_name: string; student_roll: string | null; degree: string | null; college: string | null;
  registration_no: string | null;
  last_exam_name_and_roll: string | null;
  passed_from_institution: string | null;
  fee_payment_date: string | null;
  migration_reason: string | null;
  address: string | null;
  receipt: ReceiptInfo | null;
  decided_by: string | null; decided_at: string | null; decision_remark: string | null;
  submitted_at: string | null; created_at: string | null;
  is_owner: boolean; can_edit: boolean;
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
  submitted: "bg-amber-100 text-amber-800",
  approved: "bg-green-100 text-green-700",
  rejected: "bg-red-100 text-red-700",
};

export function MigrationStatusBadge({ status, label }: { status: string; label: string }) {
  const style = STATUS_STYLE[status] ?? "bg-gray-100 text-gray-700";
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${style}`}>{label}</span>;
}

export function StudentInfoCard({ m }: { m: MigrationDetail }) {
  const rows: [string, string | null][] = [
    ["Name", m.student_name], ["Roll No", m.student_roll], ["Degree", m.degree], ["College", m.college],
  ];
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
      {rows.map(([label, value]) => (
        <p key={label}><span className="font-semibold text-gray-700">{label}:</span> <span className="text-gray-900">{value || "—"}</span></p>
      ))}
    </div>
  );
}

export function DecisionNotice({ m }: { m: MigrationDetail }) {
  if (m.status !== "approved" && m.status !== "rejected") return null;
  const isApproved = m.status === "approved";
  return (
    <div className={`rounded-2xl border-2 p-5 ${isApproved ? "border-green-300 bg-green-50" : "border-red-300 bg-red-50"}`} role="status">
      <h2 className={`text-lg font-bold mb-2 ${isApproved ? "text-green-800" : "text-red-800"}`}>
        {isApproved ? "Migration Application Approved" : "Migration Application Rejected"}
      </h2>
      <p className={`text-sm ${isApproved ? "text-green-900" : "text-red-900"}`}>
        Decided by {m.decided_by ?? "the Registrar"} on {formatDateTime(m.decided_at)}.
      </p>
      {m.decision_remark && (
        <>
          <p className={`text-sm font-semibold mt-3 ${isApproved ? "text-green-900" : "text-red-900"}`}>Remark</p>
          <p className={`whitespace-pre-wrap text-base ${isApproved ? "text-green-950" : "text-red-950"}`} data-testid="migration-decision-remark">{m.decision_remark}</p>
        </>
      )}
      {!isApproved && <p className="text-sm text-red-900 mt-3">This application is final. You may create a new Migration application.</p>}
    </div>
  );
}
