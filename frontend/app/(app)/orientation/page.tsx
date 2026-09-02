"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  ClipboardCheck, Plus, X, Check, Ban, KeyRound, RotateCw,
} from "lucide-react";

interface Program { id: string; name: string; code: string; }
interface Candidate {
  id: string; name: string; personal_email: string; mobile: string | null;
  entrance_exam_name: string | null; entrance_exam_marks: number | null;
  academic_year: string; program_id: string; program_name: string | null; program_code: string | null;
  attendance_status: "pending" | "present" | "absent";
  selection_status: "pending" | "selected" | "not_selected";
  credential_status: "not_generated" | "generated" | "sent" | "failed";
  roll_no: string | null; created_at: string;
}

const ATTENDANCE_LABEL: Record<string, string> = { pending: "Pending", present: "Present", absent: "Absent" };
const ATTENDANCE_STYLE: Record<string, string> = {
  pending: "bg-gray-100 text-gray-600", present: "bg-green-100 text-green-700", absent: "bg-red-100 text-red-700",
};
const SELECTION_LABEL: Record<string, string> = { pending: "Pending", selected: "Selected", not_selected: "Not Selected" };
const SELECTION_STYLE: Record<string, string> = {
  pending: "bg-gray-100 text-gray-600", selected: "bg-teal-100 text-teal-700", not_selected: "bg-red-100 text-red-700",
};
const CREDENTIAL_LABEL: Record<string, string> = {
  not_generated: "—", generated: "Credentials Generated", sent: "Credentials Sent", failed: "Email Failed",
};
const CREDENTIAL_STYLE: Record<string, string> = {
  not_generated: "bg-gray-100 text-gray-500", generated: "bg-blue-100 text-blue-700",
  sent: "bg-green-100 text-green-700", failed: "bg-amber-100 text-amber-700",
};

const EMPTY_FORM = { name: "", personal_email: "", mobile: "", entrance_exam_name: "", entrance_exam_marks: "", academic_year: String(new Date().getFullYear()), program_id: "" };

export default function OrientationPage() {
  const qc = useQueryClient();
  const [academicYear, setAcademicYear] = useState(String(new Date().getFullYear()));
  const [programId, setProgramId] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Candidate | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [confirmAction, setConfirmAction] = useState<{ type: "select" | "reject"; candidate: Candidate } | null>(null);

  const { data: programs = [] } = useQuery<Program[]>({
    queryKey: ["ams-programs"],
    queryFn: async () => (await api.get("/departments/programs")).data,
  });

  const { data: candidates = [], isLoading } = useQuery<Candidate[]>({
    queryKey: ["ams-orientation-candidates", academicYear, programId],
    queryFn: async () => (await api.get("/orientation/candidates", {
      params: { academic_year: academicYear || undefined, program_id: programId || undefined },
    })).data,
  });

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["ams-orientation-candidates"] });
  }

  const saveCandidate = useMutation({
    mutationFn: () => {
      const body = {
        name: form.name, personal_email: form.personal_email, mobile: form.mobile || null,
        entrance_exam_name: form.entrance_exam_name || null,
        entrance_exam_marks: form.entrance_exam_marks ? Number(form.entrance_exam_marks) : null,
        academic_year: form.academic_year, program_id: form.program_id,
      };
      return editing ? api.put(`/orientation/candidates/${editing.id}`, body) : api.post("/orientation/candidates", body);
    },
    onSuccess: () => {
      toast.success(editing ? "Candidate updated." : "Candidate added.");
      invalidate(); closeModal();
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to save candidate."),
  });

  const markAttendance = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "present" | "absent" }) =>
      api.patch(`/orientation/candidates/${id}/attendance`, null, { params: { status } }),
    onSuccess: () => { toast.success("Attendance updated."); invalidate(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to update attendance."),
  });

  const decideSelection = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "selected" | "not_selected" }) =>
      api.patch(`/orientation/candidates/${id}/selection`, null, { params: { status } }),
    onSuccess: (res, { status }) => {
      if (status === "selected") {
        toast.success(`Selected. Roll No. ${res.data.roll_no} — ${res.data.credential_status === "sent" ? "credentials emailed." : "credential email failed, use Resend."}`);
      } else {
        toast.success("Candidate marked as not selected.");
      }
      invalidate(); setConfirmAction(null);
    },
    onError: (e: unknown) => { toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to update selection."); setConfirmAction(null); },
  });

  const resend = useMutation({
    mutationFn: (id: string) => api.post(`/orientation/candidates/${id}/resend-credentials`),
    onSuccess: (res) => {
      toast.success(res.data.credential_status === "sent" ? "Credentials resent." : "Account updated, but email failed.");
      invalidate();
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to resend credentials."),
  });

  function openAdd() { setEditing(null); setForm({ ...EMPTY_FORM, academic_year: academicYear, program_id: programId }); setModalOpen(true); }
  function openEdit(c: Candidate) {
    setEditing(c);
    setForm({
      name: c.name, personal_email: c.personal_email, mobile: c.mobile ?? "",
      entrance_exam_name: c.entrance_exam_name ?? "", entrance_exam_marks: c.entrance_exam_marks?.toString() ?? "",
      academic_year: c.academic_year, program_id: c.program_id,
    });
    setModalOpen(true);
  }
  function closeModal() { setModalOpen(false); setEditing(null); setForm(EMPTY_FORM); }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2">
            <ClipboardCheck size={24} className="text-[#0D6E6E]" /> Orientation / Student Intake
          </h1>
          <p className="text-gray-700 text-base mt-1">Mark attendance, select candidates, and issue AMS student accounts</p>
        </div>
        <button onClick={openAdd} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F]">
          <Plus size={16} /> Add Candidate
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-4">
        <div>
          <label className="block text-sm font-semibold text-gray-700 mb-1">Academic Year</label>
          <input value={academicYear} onChange={(e) => setAcademicYear(e.target.value)} placeholder="e.g. 2026"
            className="border border-gray-300 rounded-xl px-3 py-2 text-base w-40 focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
        </div>
        <div>
          <label className="block text-sm font-semibold text-gray-700 mb-1">Programme</label>
          <select value={programId} onChange={(e) => setProgramId(e.target.value)}
            className="border border-gray-300 rounded-xl px-3 py-2 text-base w-64 focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
            <option value="">All Programmes</option>
            {programs.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.code})</option>)}
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
        {isLoading ? (
          <div className="flex justify-center py-20 text-gray-400">Loading…</div>
        ) : candidates.length === 0 ? (
          <div className="text-center py-20 text-gray-400">
            <ClipboardCheck size={40} className="mx-auto mb-3 opacity-30" />
            <p className="font-medium">No candidates found for this filter</p>
          </div>
        ) : (
          <table className="w-full text-sm min-w-[1100px]">
            <thead className="bg-gray-50 border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                {["Name", "Email / Mobile", "Programme", "Entrance", "Attendance", "Selection", "Roll No.", "Credentials", "Actions"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {candidates.map((c, i) => (
                <tr key={c.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 font-semibold text-gray-900 whitespace-nowrap">{c.name}</td>
                  <td className="px-4 py-3">
                    <p className="text-gray-700">{c.personal_email}</p>
                    <p className="text-gray-400 text-xs">{c.mobile ?? "—"}</p>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">{c.program_code ?? "—"}</td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    {c.entrance_exam_name ? `${c.entrance_exam_name} (${c.entrance_exam_marks ?? "—"})` : "—"}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold ${ATTENDANCE_STYLE[c.attendance_status]}`}>
                      {ATTENDANCE_LABEL[c.attendance_status]}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold ${SELECTION_STYLE[c.selection_status]}`}>
                      {SELECTION_LABEL[c.selection_status]}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs font-bold text-teal-700 whitespace-nowrap">{c.roll_no ?? "—"}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold whitespace-nowrap ${CREDENTIAL_STYLE[c.credential_status]}`}>
                      {CREDENTIAL_LABEL[c.credential_status]}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1.5">
                      {c.selection_status === "pending" && (
                        <>
                          <button onClick={() => openEdit(c)} className="px-2.5 py-1 text-xs font-semibold border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50">Edit</button>
                          {c.attendance_status !== "present" && (
                            <button onClick={() => markAttendance.mutate({ id: c.id, status: "present" })}
                              className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold border border-green-300 text-green-700 rounded-lg hover:bg-green-50">
                              <Check size={11} /> Present
                            </button>
                          )}
                          {c.attendance_status !== "absent" && (
                            <button onClick={() => markAttendance.mutate({ id: c.id, status: "absent" })}
                              className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold border border-red-300 text-red-700 rounded-lg hover:bg-red-50">
                              <Ban size={11} /> Absent
                            </button>
                          )}
                          {c.attendance_status === "present" && (
                            <button onClick={() => setConfirmAction({ type: "select", candidate: c })}
                              className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold bg-[#0D6E6E] text-white rounded-lg hover:bg-[#178F8F]">
                              <KeyRound size={11} /> Select
                            </button>
                          )}
                          {c.attendance_status !== "pending" && (
                            <button onClick={() => setConfirmAction({ type: "reject", candidate: c })}
                              className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold border border-red-300 text-red-700 rounded-lg hover:bg-red-50">
                              <X size={11} /> Not Select
                            </button>
                          )}
                        </>
                      )}
                      {c.selection_status === "selected" && (
                        <button onClick={() => resend.mutate(c.id)} disabled={resend.isPending}
                          className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50 disabled:opacity-50">
                          <RotateCw size={11} /> Resend
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Add/Edit Modal */}
      {modalOpen && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold text-gray-900">{editing ? "Edit Candidate" : "Add Candidate"}</h3>
              <button onClick={closeModal}><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <div className="space-y-3">
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Full Name</label>
                <input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Personal Email</label>
                <input type="email" value={form.personal_email} onChange={(e) => setForm((f) => ({ ...f, personal_email: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Mobile</label>
                <input value={form.mobile} onChange={(e) => setForm((f) => ({ ...f, mobile: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Entrance Exam</label>
                  <input value={form.entrance_exam_name} onChange={(e) => setForm((f) => ({ ...f, entrance_exam_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Marks</label>
                  <input type="number" value={form.entrance_exam_marks} onChange={(e) => setForm((f) => ({ ...f, entrance_exam_marks: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Academic Year</label>
                  <input value={form.academic_year} onChange={(e) => setForm((f) => ({ ...f, academic_year: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Programme</label>
                  <select value={form.program_id} onChange={(e) => setForm((f) => ({ ...f, program_id: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">Select…</option>
                    {programs.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.code})</option>)}
                  </select>
                </div>
              </div>
            </div>
            <button
              onClick={() => saveCandidate.mutate()}
              disabled={saveCandidate.isPending || !form.name || !form.personal_email || !form.academic_year || !form.program_id}
              className="w-full mt-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50">
              {saveCandidate.isPending ? "Saving…" : editing ? "Save Changes" : "Add Candidate"}
            </button>
          </div>
        </div>
      )}

      {/* Confirm Select/Reject */}
      {confirmAction && (
        <ConfirmDialog
          title={confirmAction.type === "select" ? "Select Candidate" : "Mark as Not Selected"}
          message={
            confirmAction.type === "select"
              ? `This will generate a roll number, create an AVFU AMS student account for ${confirmAction.candidate.name}, and email login credentials to ${confirmAction.candidate.personal_email}. This cannot be undone.`
              : `${confirmAction.candidate.name} will be marked as not selected. No account or roll number will be generated.`
          }
          confirmLabel={confirmAction.type === "select" ? "Yes, Select & Create Account" : "Yes, Mark Not Selected"}
          confirmClassName={confirmAction.type === "select" ? "bg-[#0D6E6E] hover:bg-[#178F8F] text-white" : "bg-red-600 hover:bg-red-700 text-white"}
          onCancel={() => setConfirmAction(null)}
          onConfirm={() => decideSelection.mutate({ id: confirmAction.candidate.id, status: confirmAction.type === "select" ? "selected" : "not_selected" })}
        />
      )}
    </div>
  );
}
