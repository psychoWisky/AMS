"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { FileSpreadsheet, Plus, X, Loader2, Search, CheckCircle2, Clock, RotateCcw, Download } from "lucide-react";

interface PpwCourseRow {
  id: string; sl_no: number; course_id: string; course_number: string | null;
  course_title: string | null; credit_structure: string | null; credits: number;
  department_name: string | null;
}
interface ClassificationSummary {
  classification: string; label: string; required_credits: number;
  selected_credits: number; remaining_credits: number; courses: PpwCourseRow[];
}
interface PpwHeader {
  student_name: string; student_roll: string | null; program_name: string | null;
  program_level: string | null; department_name: string | null; college_name: string | null;
  admission_year: number | null;
}
// signature_status: "not_applicable" | "not_submitted" | "pending" | "approved" | "reverted"
interface CommitteeRow {
  category: string; faculty_name: string | null; designation: string | null;
  department_name: string | null; signature_status: string; signed_at: string | null;
  is_current_stage: boolean; remark: string | null;
}
interface HodApproval {
  status: string; approver_name: string | null; department_name: string | null;
  signed_at: string | null; is_current_stage: boolean; remark: string | null;
}
interface Ppw {
  id: string; status: string;
  field_of_investigation: string | null; minor_field: string | null;
  supporting_field: string | null; research_title: string | null;
  submitted_at: string | null;
  classifications: ClassificationSummary[];
  header: PpwHeader;
  committee: { committee_found: boolean; cycle_number: number | null; cycle_status: string | null; revert_remark: string | null; reverted_at: string | null; rows: CommitteeRow[] };
  hod_approval: HodApproval;
  signatures: { head: string; dpgs: string };
  is_editable: boolean;
}
interface AvailableCourse {
  id: string; course_number: string; title: string; credit_structure: string;
  credits: number; department_id: string | null; department_name: string | null;
}

const CLASSIFICATION_ORDER = ["major", "minor", "supporting", "research", "seminar", "compulsory"];

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft", major_advisor_pending: "Awaiting Major Advisor Approval",
  committee_pending: "Awaiting Advisory Committee Approval", hod_pending: "Awaiting HOD Approval",
  hod_approved: "Approved by HOD", reverted: "Reverted — Needs Correction",
};

function SignatureBadge({ status }: { status: string }) {
  if (status === "approved") return <span className="inline-flex items-center gap-1 text-green-700 text-sm font-semibold"><CheckCircle2 size={15} /> Approved</span>;
  if (status === "reverted") return <span className="inline-flex items-center gap-1 text-red-600 text-sm font-semibold"><RotateCcw size={15} /> Reverted</span>;
  if (status === "pending") return <span className="inline-flex items-center gap-1 text-amber-600 text-sm font-semibold"><Clock size={15} /> Pending</span>;
  if (status === "not_applicable") return <span className="text-gray-400 text-sm">Not applicable</span>;
  return <span className="text-gray-400 text-sm">—</span>;
}

export default function PpwPage() {
  const qc = useQueryClient();

  const { data: ppw, isLoading, isError } = useQuery<Ppw>({
    queryKey: ["ams-my-ppw"],
    queryFn: async () => (await api.get("/ppw/me")).data,
    retry: false,
  });

  const createPpw = useMutation({
    mutationFn: () => api.post("/ppw", {}),
    onSuccess: () => { toast.success("PPW draft created."); qc.invalidateQueries({ queryKey: ["ams-my-ppw"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to create PPW."),
  });

  if (isLoading) return <div className="flex justify-center py-24"><Loader2 className="animate-spin text-gray-600" /></div>;

  if (isError || !ppw) {
    return (
      <div className="p-6 max-w-3xl mx-auto text-center py-24">
        <FileSpreadsheet size={40} className="mx-auto mb-4 text-gray-300" />
        <h1 className="text-2xl font-bold text-gray-900 mb-2">PPW — Proposed Programme of Work</h1>
        <p className="text-gray-600 mb-6">You have not started your PPW yet.</p>
        <button onClick={() => createPpw.mutate()} disabled={createPpw.isPending}
          className="px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-bold hover:bg-[#178F8F] disabled:opacity-60">
          {createPpw.isPending ? "Creating…" : "Start PPW"}
        </button>
      </div>
    );
  }

  // Keying by id+status re-mounts PpwEditor whenever the PPW identity or lock
  // state changes, so its form state can be derived once via useState's lazy
  // initializer instead of synced in an effect (matches student-management/page.tsx's
  // established pattern in this codebase).
  return <PpwEditor key={`${ppw.id}-${ppw.status}`} ppw={ppw} />;
}

function PpwEditor({ ppw }: { ppw: Ppw }) {
  const qc = useQueryClient();
  const [addingFor, setAddingFor] = useState<string | null>(null);
  const [courseSearch, setCourseSearch] = useState("");
  // Phase 2: editable while draft OR reverted (student may correct and resubmit) —
  // was "draft"-only in Phase 1. `is_editable` is server-computed (ppw.py's
  // _EDITABLE_STATUSES), never re-derived here, so the UI can never drift from
  // what the backend actually allows.
  const isDraft = ppw.is_editable;
  const isReverted = ppw.status === "reverted";

  const { data: availableCourses = [] } = useQuery<AvailableCourse[]>({
    queryKey: ["ams-ppw-available-courses"],
    queryFn: async () => (await api.get("/ppw/available-courses")).data,
    enabled: !!addingFor,
  });

  const [form, setForm] = useState(() => ({
    field_of_investigation: ppw.field_of_investigation ?? "",
    minor_field: ppw.minor_field ?? "",
    supporting_field: ppw.supporting_field ?? "",
    research_title: ppw.research_title ?? "",
  }));

  const saveDraft = useMutation({
    mutationFn: () => api.patch(`/ppw/${ppw.id}`, form),
    onSuccess: () => { toast.success("Draft saved."); qc.invalidateQueries({ queryKey: ["ams-my-ppw"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to save."),
  });

  // Phase 3 — downloads the official PPW PDF (server-rendered, AMS-owned
  // Playwright pipeline). Response is a binary blob, not JSON, so errors
  // arrive as a blob too — read it back as text to surface the real
  // {detail: "..."} message instead of a generic failure toast.
  const downloadDocument = useMutation({
    mutationFn: async () => {
      const res = await api.get(`/ppw/${ppw.id}/document`, { responseType: "blob" });
      const blobUrl = window.URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = blobUrl;
      // Roll numbers like "AVFU/2023/BSCAG/002" contain slashes — sanitize
      // the same way the backend's Content-Disposition filename does, so the
      // suggested download name is never browser-dependent/ambiguous.
      const safeRoll = (ppw.header.student_roll ?? ppw.id).replace(/[^a-zA-Z0-9_-]/g, "-");
      link.download = `PPW-${safeRoll}-${ppw.status}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(blobUrl);
    },
    onSuccess: () => toast.success("PPW document downloaded."),
    onError: async (e: unknown) => {
      const err = e as { response?: { data?: Blob; status?: number } };
      let message = "Failed to generate PPW document.";
      if (err.response?.data instanceof Blob) {
        try {
          const parsed = JSON.parse(await err.response.data.text());
          if (parsed?.detail) message = parsed.detail;
        } catch { /* non-JSON blob body — keep generic message */ }
      }
      toast.error(message);
    },
  });

  const addCourse = useMutation({
    mutationFn: ({ course_id, classification }: { course_id: string; classification: string }) =>
      api.post(`/ppw/${ppw.id}/courses`, { course_id, classification }),
    onSuccess: () => { toast.success("Course added."); qc.invalidateQueries({ queryKey: ["ams-my-ppw"] }); setAddingFor(null); setCourseSearch(""); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to add course."),
  });

  const removeCourse = useMutation({
    mutationFn: (ppwCourseId: string) => api.delete(`/ppw/${ppw.id}/courses/${ppwCourseId}`),
    onSuccess: () => { toast.success("Course removed."); qc.invalidateQueries({ queryKey: ["ams-my-ppw"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to remove course."),
  });

  const submitPpw = useMutation({
    mutationFn: () => api.patch(`/ppw/${ppw.id}/submit`),
    onSuccess: () => { toast.success("PPW submitted."); qc.invalidateQueries({ queryKey: ["ams-my-ppw"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to submit."),
  });

  const filteredCourses = availableCourses.filter((c) => {
    const q = courseSearch.trim().toLowerCase();
    if (!q) return true;
    return c.course_number.toLowerCase().includes(q) || c.title.toLowerCase().includes(q);
  });

  const orderedClassifications = CLASSIFICATION_ORDER
    .map((key) => ppw.classifications.find((c) => c.classification === key))
    .filter((c): c is ClassificationSummary => !!c);

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><FileSpreadsheet size={24} className="text-[#0D6E6E]" />PPW — Proposed Programme of Work</h1>
          <p className="text-gray-700 text-base mt-1">
            Status: <span className={`font-semibold ${ppw.status === "draft" ? "text-amber-700" : ppw.status === "hod_approved" ? "text-green-700" : ppw.status === "reverted" ? "text-red-600" : "text-blue-700"}`}>
              {STATUS_LABELS[ppw.status] ?? ppw.status}
            </span>
            {ppw.submitted_at && <span className="text-gray-500"> — submitted {new Date(ppw.submitted_at).toLocaleString()}</span>}
          </p>
        </div>
        <div className="flex gap-2">
          {isDraft && (
            <button onClick={() => saveDraft.mutate()} disabled={saveDraft.isPending}
              className="px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl font-semibold text-sm hover:bg-[#E6F4F4] disabled:opacity-60">
              {saveDraft.isPending ? "Saving…" : "Save Draft"}
            </button>
          )}
          {isDraft && (
            <button onClick={() => submitPpw.mutate()} disabled={submitPpw.isPending}
              className="px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-sm hover:bg-[#178F8F] disabled:opacity-60">
              {submitPpw.isPending ? (isReverted ? "Resubmitting…" : "Submitting…") : (isReverted ? "Resubmit PPW" : "Submit PPW")}
            </button>
          )}
          {ppw.status !== "draft" && (
            <button onClick={() => downloadDocument.mutate()} disabled={downloadDocument.isPending}
              className="flex items-center gap-1.5 px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl font-semibold text-sm hover:bg-[#E6F4F4] disabled:opacity-60">
              {downloadDocument.isPending ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
              {downloadDocument.isPending ? "Generating…" : "Download PPW Document"}
            </button>
          )}
        </div>
      </div>

      {isReverted && (
        <div className="bg-red-50 border border-red-200 text-red-800 rounded-2xl px-4 py-3 text-sm">
          <p className="font-semibold flex items-center gap-1.5"><RotateCcw size={15} /> Your PPW was reverted for correction.</p>
          {ppw.committee.revert_remark && <p className="mt-1">Remark: {ppw.committee.revert_remark}</p>}
          <p className="mt-1">Please make the necessary corrections above and resubmit. Resubmitting starts a new approval cycle from the Major Advisor stage.</p>
        </div>
      )}

      {!isDraft && !isReverted && (
        <div className="bg-blue-50 border border-blue-200 text-blue-800 rounded-2xl px-4 py-3 text-sm font-medium">
          This PPW has been submitted and is locked while under approval ({STATUS_LABELS[ppw.status] ?? ppw.status}). No further edits, course additions, or removals are possible until a new cycle begins.
        </div>
      )}

      {/* Section 1 — PPW Details */}
      <section className="bg-white rounded-2xl border border-gray-200 p-5">
        <h2 className="font-bold text-gray-800 mb-4">PPW Details</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {([
            ["field_of_investigation", "Field of Investigation for Thesis / Project / Dissertation *"],
            ["minor_field", "Minor Field *"],
            ["supporting_field", "Supporting Field *"],
            ["research_title", "Research Title *"],
          ] as const).map(([key, label]) => (
            <div key={key}>
              <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
              <textarea value={form[key]} disabled={!isDraft} rows={2}
                onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
                className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50 disabled:text-gray-600 resize-none" />
            </div>
          ))}
        </div>
      </section>

      {/* Section 2 — Course Plan */}
      <section className="bg-white rounded-2xl border border-gray-200 p-5">
        <h2 className="font-bold text-gray-800 mb-1">Courses to be completed to meet Post-Graduation / Ph.D Requirements</h2>
        <p className="text-sm text-gray-500 mb-4">Classification of courses</p>
        <div className="space-y-6">
          {orderedClassifications.map((c) => (
            <div key={c.classification} className="border border-gray-200 rounded-xl overflow-hidden">
              <div className="flex items-center justify-between bg-gray-50 px-4 py-3 border-b border-gray-200">
                <div>
                  <h3 className="font-bold text-gray-800">{c.label}</h3>
                  <p className="text-sm text-gray-600">
                    Selected: <span className="font-semibold">{c.selected_credits}</span> / Required: <span className="font-semibold">{c.required_credits}</span>
                    {" "}— <span className={c.remaining_credits > 0 ? "text-amber-700" : c.remaining_credits < 0 ? "text-red-600" : "text-green-700"}>
                      {c.remaining_credits > 0 ? `${c.remaining_credits} remaining` : c.remaining_credits < 0 ? `${-c.remaining_credits} over target` : "target met"}
                    </span>
                  </p>
                </div>
                {isDraft && (
                  <button onClick={() => { setAddingFor(c.classification); setCourseSearch(""); }}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-sm font-semibold hover:bg-[#178F8F]">
                    <Plus size={14} /> Add Course
                  </button>
                )}
              </div>
              {c.courses.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-5">No courses added yet.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead className="bg-gray-50/50 border-b border-gray-100">
                    <tr>{["SL NO", "Course Code", "Course Title", "Credit", "Department", "Action"].map((h) => (
                      <th key={h} className="text-left px-4 py-2 font-semibold text-gray-600">{h}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {c.courses.map((row) => (
                      <tr key={row.id} className="border-b border-gray-50 last:border-0">
                        <td className="px-4 py-2 text-gray-600">{row.sl_no}</td>
                        <td className="px-4 py-2 font-mono font-bold text-[#0D6E6E]">{row.course_number}</td>
                        <td className="px-4 py-2">{row.course_title}</td>
                        <td className="px-4 py-2 font-mono">{row.credit_structure} ({row.credits})</td>
                        <td className="px-4 py-2 text-gray-600">{row.department_name ?? "—"}</td>
                        <td className="px-4 py-2">
                          {isDraft && (
                            <button onClick={() => removeCourse.mutate(row.id)} className="text-red-500 hover:bg-red-50 p-1 rounded"><X size={15} /></button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {addingFor === c.classification && (
                <div className="border-t border-gray-200 p-4 bg-gray-50/50">
                  <div className="relative mb-2">
                    <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                    <input autoFocus value={courseSearch} onChange={(e) => setCourseSearch(e.target.value)} placeholder="Search course code or title…"
                      className="w-full pl-8 pr-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                  </div>
                  <div className="max-h-48 overflow-y-auto border border-gray-200 rounded-lg bg-white">
                    {filteredCourses.length === 0 ? (
                      <p className="text-sm text-gray-500 p-3">No matching courses.</p>
                    ) : filteredCourses.slice(0, 30).map((course) => (
                      <button key={course.id} type="button"
                        onClick={() => addCourse.mutate({ course_id: course.id, classification: c.classification })}
                        disabled={addCourse.isPending}
                        className="w-full flex items-center justify-between text-left px-3 py-2 text-sm hover:bg-[#E6F4F4] border-b border-gray-50 last:border-0 disabled:opacity-50">
                        <span><span className="font-mono font-bold text-[#0D6E6E]">{course.course_number}</span> — {course.title} <span className="text-gray-500">({course.credit_structure}, {course.department_name ?? "—"})</span></span>
                        <Plus size={14} className="text-[#0D6E6E] shrink-0" />
                      </button>
                    ))}
                  </div>
                  <button onClick={() => setAddingFor(null)} className="mt-2 text-sm text-gray-500 hover:underline">Close</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* Approval Status — quick-glance, dynamic per Section 11's requirement (distinct from the document-style preview below) */}
      {ppw.status !== "draft" && (
        <section className="bg-white rounded-2xl border border-gray-200 p-5">
          <h2 className="font-bold text-gray-800 mb-4">Approval Status</h2>
          <div className="space-y-2">
            {ppw.committee.rows.filter((r) => r.signature_status !== "not_applicable").map((row) => (
              <div key={row.category} className={`flex items-center justify-between px-3 py-2 rounded-lg ${row.is_current_stage ? "bg-amber-50 border border-amber-200" : "bg-gray-50"}`}>
                <div>
                  <p className="font-semibold text-sm text-gray-800">{row.category}</p>
                  <p className="text-xs text-gray-500">{row.faculty_name ?? "Not assigned"}{row.designation ? ` — ${row.designation}` : ""}</p>
                  {row.remark && <p className="text-xs text-red-600 mt-0.5">Remark: {row.remark}</p>}
                </div>
                <SignatureBadge status={row.signature_status} />
              </div>
            ))}
            <div className={`flex items-center justify-between px-3 py-2 rounded-lg ${ppw.hod_approval.is_current_stage ? "bg-amber-50 border border-amber-200" : "bg-gray-50"}`}>
              <div>
                <p className="font-semibold text-sm text-gray-800">HOD</p>
                <p className="text-xs text-gray-500">{ppw.hod_approval.approver_name ?? (ppw.hod_approval.department_name ? `Department: ${ppw.hod_approval.department_name}` : "Not yet reached")}</p>
                {ppw.hod_approval.remark && <p className="text-xs text-red-600 mt-0.5">Remark: {ppw.hod_approval.remark}</p>}
              </div>
              <SignatureBadge status={ppw.hod_approval.status} />
            </div>
          </div>
        </section>
      )}

      {/* Section 3 — Preview (document style) */}
      <section className="bg-white rounded-2xl border border-gray-200 p-8">
        <h2 className="font-bold text-gray-800 mb-4">PPW Preview</h2>
        <div className="border border-gray-300 rounded-xl p-8 max-w-3xl mx-auto text-sm leading-relaxed">
          <div className="text-center mb-4">
            <p className="font-bold text-base">Assam Agricultural University</p>
            <p>Faculty : Faculty of Veterinary Science</p>
            <p>College : {ppw.header.college_name ?? "—"}</p>
          </div>
          <p className="text-center font-bold text-base my-4">POST –GRADUATE PROGRAMME OF WORK (PPW)</p>
          <div className="text-center text-xs text-gray-500 mb-4">
            <p>Academic Regulation Form No.PG-11</p>
            <p>Video Clause: 2.06.04</p>
          </div>
          <p>To,<br />The Director, Post Graduate Studies,<br />AAU, Jorhat-785013</p>
          <p className="mt-4">
            This is to submit the Proposed Programme of Work of <strong>{ppw.header.student_name}</strong>
            {ppw.header.student_roll && <> (Roll No. {ppw.header.student_roll})</>}, a{" "}
            {ppw.header.program_level ?? "—"} student of <strong>{ppw.header.program_name ?? "—"}</strong> under the
            Department of <strong>{ppw.header.department_name ?? "—"}</strong>
            {ppw.header.admission_year && <> (Admission Year: {ppw.header.admission_year})</>}.
          </p>
          <table className="w-full my-4 text-sm">
            <tbody>
              <tr><td className="py-1 font-semibold w-56">Field of Investigation</td><td>{ppw.field_of_investigation || "—"}</td></tr>
              <tr><td className="py-1 font-semibold">Minor Field</td><td>{ppw.minor_field || "—"}</td></tr>
              <tr><td className="py-1 font-semibold">Supporting Field</td><td>{ppw.supporting_field || "—"}</td></tr>
              <tr><td className="py-1 font-semibold">Research Title</td><td>{ppw.research_title || "—"}</td></tr>
            </tbody>
          </table>

          <p className="font-bold mt-6 mb-2">Courses to be completed by the student to meet Post-Graduation / Ph.D Requirements</p>
          {orderedClassifications.map((c) => (
            <div key={c.classification} className="mb-3">
              <p className="font-semibold">{c.label} — {c.required_credits} Credits (Selected: {c.selected_credits})</p>
              {c.courses.length > 0 && (
                <table className="w-full text-xs border border-gray-200 mt-1">
                  <thead><tr className="bg-gray-50">{["SL NO", "Course Code", "Course Title", "Credit", "Department"].map((h) => <th key={h} className="border border-gray-200 px-2 py-1 text-left">{h}</th>)}</tr></thead>
                  <tbody>
                    {c.courses.map((row) => (
                      <tr key={row.id}>
                        <td className="border border-gray-200 px-2 py-1">{row.sl_no}</td>
                        <td className="border border-gray-200 px-2 py-1 font-mono">{row.course_number}</td>
                        <td className="border border-gray-200 px-2 py-1">{row.course_title}</td>
                        <td className="border border-gray-200 px-2 py-1">{row.credit_structure}</td>
                        <td className="border border-gray-200 px-2 py-1">{row.department_name ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ))}

          <p className="font-bold mt-6 mb-2">Endorsement of Students Advisory Committee</p>
          <table className="w-full text-xs border border-gray-300">
            <thead><tr className="bg-gray-50">{["Advisory Committee", "Name & Designation", "Department", "Signature", "Signed On"].map((h) => <th key={h} className="border border-gray-300 px-2 py-1 text-left">{h}</th>)}</tr></thead>
            <tbody>
              {ppw.committee.rows.map((row) => (
                <tr key={row.category} className={row.is_current_stage ? "bg-amber-50" : undefined}>
                  <td className="border border-gray-300 px-2 py-1 font-semibold">{row.category}</td>
                  <td className="border border-gray-300 px-2 py-1">{row.faculty_name ? `${row.faculty_name}${row.designation ? ` (${row.designation})` : ""}` : "Not assigned"}</td>
                  <td className="border border-gray-300 px-2 py-1">{row.department_name ?? "—"}</td>
                  <td className="border border-gray-300 px-2 py-1"><SignatureBadge status={row.signature_status} />{row.remark && <p className="text-red-600 mt-0.5">{row.remark}</p>}</td>
                  <td className="border border-gray-300 px-2 py-1 text-gray-500">{row.signed_at ? new Date(row.signed_at).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!ppw.committee.committee_found && (
            <p className="text-xs text-gray-500 mt-1">No Advisory Committee found for this student yet.</p>
          )}

          <div className="grid grid-cols-2 gap-8 mt-10 text-center">
            <div>
              <div className="border-t border-gray-400 pt-2 flex flex-col items-center gap-1">
                <SignatureBadge status={ppw.hod_approval.status} />
                {ppw.hod_approval.approver_name && <p className="text-xs text-gray-600">{ppw.hod_approval.approver_name}{ppw.hod_approval.department_name ? ` — ${ppw.hod_approval.department_name}` : ""}</p>}
                {ppw.hod_approval.signed_at && <p className="text-xs text-gray-400">{new Date(ppw.hod_approval.signed_at).toLocaleString()}</p>}
              </div>
              <p className="font-semibold mt-1">Signature of the Head</p>
            </div>
            <div>
              <p className="border-t border-gray-400 pt-2 text-gray-400 text-sm">Not implemented in this phase</p>
              <p className="font-semibold mt-1">Signature of the D.P.G.S</p>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
