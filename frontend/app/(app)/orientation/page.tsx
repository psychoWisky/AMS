"use client";
import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useUser } from "@/stores/auth.store";
import {
  ClipboardCheck, Plus, X, Check, Ban, KeyRound, RotateCw, Upload, Download,
} from "lucide-react";

interface Program { id: string; name: string; code: string; }
interface DepartmentOpt { id: string; name: string; code: string; }
interface CollegeOpt { id: string; name: string; code: string; }
interface Candidate {
  id: string; name: string;
  first_name: string | null; middle_name: string | null; last_name: string | null;
  personal_email: string; mobile: string | null;
  // AVFU Email (distinct from personal_email) — issued by IT ahead of
  // Orientation; becomes the created student's AMS login email.
  avfu_email: string | null;
  academic_year: string;
  college_id: string | null; college_name: string | null;
  program_id: string; program_name: string | null; program_code: string | null;
  // Programme<->Department many-to-many redesign — a candidate's academic
  // identity is Programme + Department together now.
  department_id: string | null; department_name: string | null;
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

const EMPTY_FORM = {
  first_name: "", middle_name: "", last_name: "",
  personal_email: "", mobile: "", avfu_email: "",
  academic_year: String(new Date().getFullYear()),
  college_id: "", program_id: "", department_id: "",
};

interface BulkUploadError { row: number; errors: string[]; }
interface BulkUploadResult { success: boolean; imported_count: number; filename?: string; errors?: BulkUploadError[]; }

export default function OrientationPage() {
  const qc = useQueryClient();
  const user = useUser();
  // Bulk upload (this task's confirmed requirement) — Super Admin only,
  // enforced independently by the backend; this is only a UI convenience.
  const isSuperAdmin = user?.role === "super_admin";
  const [academicYear, setAcademicYear] = useState(String(new Date().getFullYear()));
  const [programId, setProgramId] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Candidate | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [confirmAction, setConfirmAction] = useState<{ type: "select" | "reject"; candidate: Candidate } | null>(null);
  const [bulkModalOpen, setBulkModalOpen] = useState(false);
  const [bulkFile, setBulkFile] = useState<File | null>(null);
  const [bulkResult, setBulkResult] = useState<BulkUploadResult | null>(null);
  const bulkFileInputRef = useRef<HTMLInputElement>(null);

  const { data: programs = [] } = useQuery<Program[]>({
    queryKey: ["ams-programs"],
    queryFn: async () => (await api.get("/departments/programs")).data,
  });

  // College (this task's confirmed requirement) — reuses the existing
  // ams_colleges master-data endpoint, not a new concept.
  const { data: colleges = [] } = useQuery<CollegeOpt[]>({
    queryKey: ["ams-colleges"],
    queryFn: async () => (await api.get("/admin/colleges")).data,
  });

  // Programme<->Department many-to-many redesign — Department options are
  // filtered to whatever is associated with the selected Programme in the
  // Add/Edit modal (never the reverse — a Department can belong to several
  // Programmes, so there is no "the" Programme for a Department to filter by).
  const modalDeptQuery = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments-for-program", form.program_id],
    queryFn: async () => (await api.get("/departments", { params: { program_id: form.program_id } })).data,
    enabled: modalOpen && !!form.program_id,
  });
  const modalDepartments = modalDeptQuery.data ?? [];

  // Clear an out-of-date Department selection once the filtered list for the
  // newly-chosen Programme has actually loaded (guarded on isSuccess so this
  // never fires against the query's transient empty default and wipes a
  // valid pre-existing selection when opening Edit).
  useEffect(() => {
    if (modalDeptQuery.isSuccess && form.program_id && form.department_id && !modalDepartments.some((d) => d.id === form.department_id)) {
      setForm((f) => ({ ...f, department_id: "" }));
    }
  }, [modalDeptQuery.isSuccess, modalDepartments, form.program_id, form.department_id]);

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
        first_name: form.first_name, middle_name: form.middle_name || null, last_name: form.last_name,
        personal_email: form.personal_email, mobile: form.mobile, avfu_email: form.avfu_email,
        academic_year: form.academic_year, college_id: form.college_id,
        program_id: form.program_id, department_id: form.department_id,
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

  // Bulk upload (this task's confirmed requirement) — downloads the backend-
  // generated .xlsx template (same pattern as the PPW document blob download
  // above) so the frontend never has to construct Excel files itself.
  const downloadTemplate = useMutation({
    mutationFn: async () => {
      const res = await api.get("/orientation/candidates/bulk-upload/template", { responseType: "blob" });
      const blobUrl = window.URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = "orientation_candidates_template.xlsx";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(blobUrl);
    },
    onError: () => toast.error("Failed to download template."),
  });

  const bulkUpload = useMutation({
    mutationFn: async () => {
      if (!bulkFile) throw new Error("No file selected.");
      const fd = new FormData();
      fd.append("file", bulkFile);
      // The shared `api` instance sets a default Content-Type: application/json
      // header (services/api.ts). Axios only auto-generates the correct
      // multipart/form-data boundary header when NO Content-Type is already
      // present — since one already is (the JSON default), it is left as-is
      // and the FormData body never gets serialized as multipart at all. The
      // request then reaches FastAPI with no parseable `file` field, which
      // rejects it with 422 before any endpoint/validation code ever runs.
      // Explicitly unsetting it here for this one request lets axios compute
      // the correct multipart Content-Type + boundary itself.
      return api.post<BulkUploadResult>("/orientation/candidates/bulk-upload", fd, {
        headers: { "Content-Type": undefined },
      });
    },
    onSuccess: (res) => {
      if (res.data.success) {
        toast.success(`Successfully imported ${res.data.imported_count} candidates.`);
        invalidate();
        closeBulkModal();
      } else {
        setBulkResult(res.data);
      }
    },
    onError: (e: unknown) => {
      // Validation failures come back as a normal error response (400/409)
      // carrying the SAME structured { success, imported_count, errors }
      // shape as a successful call — surface it in the same result panel
      // rather than only a generic toast.
      const data = (e as { response?: { data?: BulkUploadResult } })?.response?.data;
      if (data && typeof data.success === "boolean") {
        setBulkResult(data);
      } else {
        toast.error("Upload failed. No candidates were imported.");
      }
    },
  });

  function closeBulkModal() {
    setBulkModalOpen(false);
    setBulkFile(null);
    setBulkResult(null);
    if (bulkFileInputRef.current) bulkFileInputRef.current.value = "";
  }

  function openAdd() { setEditing(null); setForm({ ...EMPTY_FORM, academic_year: academicYear, program_id: programId }); setModalOpen(true); }
  function openEdit(c: Candidate) {
    setEditing(c);
    setForm({
      first_name: c.first_name ?? "", middle_name: c.middle_name ?? "", last_name: c.last_name ?? "",
      personal_email: c.personal_email, mobile: c.mobile ?? "", avfu_email: c.avfu_email ?? "",
      academic_year: c.academic_year, college_id: c.college_id ?? "",
      program_id: c.program_id, department_id: c.department_id ?? "",
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
        <div className="flex items-center gap-2">
          {/* Bulk upload (this task's confirmed requirement) — Super Admin
              only; the backend independently enforces this regardless of
              what the frontend shows. */}
          {isSuperAdmin && (
            <>
              <button onClick={() => downloadTemplate.mutate()} disabled={downloadTemplate.isPending}
                className="flex items-center gap-2 px-4 py-2.5 border border-gray-300 text-gray-700 rounded-xl text-base font-bold hover:bg-gray-50 disabled:opacity-50">
                <Download size={16} /> Download Template
              </button>
              <button onClick={() => setBulkModalOpen(true)}
                className="flex items-center gap-2 px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl text-base font-bold hover:bg-teal-50">
                <Upload size={16} /> Bulk Upload
              </button>
            </>
          )}
          <button onClick={openAdd} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F]">
            <Plus size={16} /> Add Candidate
          </button>
        </div>
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
                {["Name", "Email / Mobile", "University / AVFU Email", "Programme", "Department", "Attendance", "Selection", "Roll No.", "Credentials", "Actions"].map((h) => (
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
                  <td className="px-4 py-3 whitespace-nowrap">{c.avfu_email ?? "—"}</td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    {c.program_code ?? "—"}
                    {c.college_name && <p className="text-gray-400 text-xs font-normal">{c.college_name}</p>}
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">{c.department_name ?? "—"}</td>
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
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">First Name *</label>
                  <input value={form.first_name} onChange={(e) => setForm((f) => ({ ...f, first_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Middle Name</label>
                  <input value={form.middle_name} onChange={(e) => setForm((f) => ({ ...f, middle_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Last Name *</label>
                <input value={form.last_name} onChange={(e) => setForm((f) => ({ ...f, last_name: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Personal Email *</label>
                <input type="email" value={form.personal_email} onChange={(e) => setForm((f) => ({ ...f, personal_email: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Mobile *</label>
                <input value={form.mobile} onChange={(e) => setForm((f) => ({ ...f, mobile: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">AVFU Email *</label>
                <input type="email" value={form.avfu_email} onChange={(e) => setForm((f) => ({ ...f, avfu_email: e.target.value }))}
                  placeholder="student@avfu.ac.in"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                <p className="text-xs text-gray-400 mt-1">Issued by IT for this shortlisted student — becomes their AMS login email on selection.</p>
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Academic Year *</label>
                <input value={form.academic_year} onChange={(e) => setForm((f) => ({ ...f, academic_year: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">College *</label>
                <select value={form.college_id} onChange={(e) => setForm((f) => ({ ...f, college_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select…</option>
                  {colleges.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.code})</option>)}
                </select>
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Programme *</label>
                <select value={form.program_id} onChange={(e) => setForm((f) => ({ ...f, program_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select…</option>
                  {programs.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.code})</option>)}
                </select>
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Department *</label>
                <select value={form.department_id} onChange={(e) => setForm((f) => ({ ...f, department_id: e.target.value }))}
                  disabled={!form.program_id}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50">
                  <option value="">{form.program_id ? "Select…" : "Select a Programme first"}</option>
                  {modalDepartments.map((d) => <option key={d.id} value={d.id}>{d.name} ({d.code})</option>)}
                </select>
                {form.program_id && modalDepartments.length === 0 && (
                  <p className="text-xs text-amber-600 mt-1">No Departments are associated with this Programme yet.</p>
                )}
              </div>
            </div>
            <button
              onClick={() => saveCandidate.mutate()}
              disabled={saveCandidate.isPending || !form.first_name || !form.last_name || !form.personal_email || !form.mobile || !form.avfu_email || !form.academic_year || !form.college_id || !form.program_id || !form.department_id}
              className="w-full mt-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50">
              {saveCandidate.isPending ? "Saving…" : editing ? "Save Changes" : "Add Candidate"}
            </button>
          </div>
        </div>
      )}

      {/* Bulk Upload Modal (this task's confirmed requirement, Super Admin only) */}
      {bulkModalOpen && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold text-gray-900">Bulk Upload Candidates</h3>
              <button onClick={closeBulkModal}><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <p className="text-sm text-gray-600 mb-3">
              Upload an <span className="font-semibold">.xlsx</span> or <span className="font-semibold">.csv</span> file
              with columns: First Name, Middle Name (optional), Last Name, Personal Email, Mobile, AVFU Email,
              Academic Year, College, Programme, Department. College/Programme/Department are matched by their
              exact existing name. Use <span className="font-semibold">Download Template</span> to get the exact format.
            </p>
            <div>
              <input ref={bulkFileInputRef} type="file" accept=".xlsx,.csv"
                onChange={(e) => { setBulkFile(e.target.files?.[0] ?? null); setBulkResult(null); }}
                className="w-full text-sm border border-gray-300 rounded-xl px-3 py-2 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:bg-gray-100 file:text-gray-700 file:font-semibold" />
              {bulkFile && <p className="text-xs text-gray-500 mt-1">Selected: {bulkFile.name}</p>}
            </div>

            {bulkResult && !bulkResult.success && (
              <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-xl">
                <p className="text-sm font-bold text-red-700 mb-2">Upload failed. No candidates were imported.</p>
                <div className="max-h-56 overflow-y-auto space-y-2 pr-1">
                  {(bulkResult.errors ?? []).map((e, i) => (
                    <div key={i} className="text-sm">
                      <p className="font-semibold text-gray-800">{e.row > 0 ? `Row ${e.row}` : "Upload"}</p>
                      <ul className="list-disc list-inside text-red-700">
                        {e.errors.map((msg, j) => <li key={j}>{msg}</li>)}
                      </ul>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="flex gap-3 mt-5">
              <button onClick={closeBulkModal}
                className="flex-1 py-2.5 border border-gray-300 text-gray-700 rounded-xl text-base font-bold hover:bg-gray-50">
                Cancel
              </button>
              <button onClick={() => bulkUpload.mutate()} disabled={!bulkFile || bulkUpload.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50">
                {bulkUpload.isPending ? "Uploading…" : "Upload"}
              </button>
            </div>
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
