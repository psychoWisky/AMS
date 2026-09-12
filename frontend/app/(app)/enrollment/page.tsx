"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import Link from "next/link";
import { ClipboardList, CheckCircle2, XCircle, Loader2, ArrowRight, Eye, X, Download, FileText } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { COURSE_CATEGORY_LABELS, CREDIT_TYPE_LABELS } from "@/lib/utils";

// My Courses task (this revision) — the confirmed BUSINESS_LOGIC.md K.7
// column set (SL No/Course Type/Course Number/Course Title/Credit/Credit
// Type/Status/Action), backed by the same `ams_student_enrollments` table
// `GET /enrollment/my` already read — now with Academic Year/Semester
// filters and a registration-stage-aware Status, since a single item-level
// status alone can no longer describe "Major Advisor Pending"/"HOD Pending".
interface MyCourseRow {
  id: string; offering_id: string; registration_id: string | null; registration_stage: string | null;
  course_number: string; course_title: string; credit_structure: string; credits: number;
  category: string | null; credit_type: string | null; section: string | null;
  semester_name: string | null; academic_year: string | null; calendar_id: string | null; semester_id: string | null;
  teachers: string[]; status: string; status_label: string; enrolled_at: string; remarks: string | null;
  withdrawal_request: { status: string; status_label: string } | null;
}
interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface Offering { id: string; course_number: string; course_title: string; credit_structure: string; section: string | null; max_enrollment: number; enrolled_count: number; status: string; faculty_names: string[]; department_id: string | null; department_name: string | null; }
interface EnrollmentRow { id: string; student_id: string; student_name: string; student_roll: string; status: string; enrolled_at: string; remarks: string | null; }

const STATUS_COLOR: Record<string, string> = { pending: "bg-amber-100 text-amber-700", approved: "bg-green-100 text-green-700", rejected: "bg-red-100 text-red-700", withdrawn: "bg-gray-100 text-gray-600" };
const REG_STATUS_COLOR: Record<string, string> = {
  "Course Teacher Approval Pending": "bg-amber-100 text-amber-700",
  "Course Teacher Approved": "bg-green-100 text-green-700",
  "Ready for Registration Card Submission": "bg-teal-100 text-teal-700",
  "Major Advisor Approval Pending": "bg-blue-100 text-blue-700",
  "HOD Approval Pending": "bg-purple-100 text-purple-700",
  "HOD Approved": "bg-green-100 text-green-700",
  "Reverted": "bg-red-100 text-red-700",
  "Withdrawn": "bg-gray-100 text-gray-600",
};

function CourseDetailsModal({ row, onClose }: { row: MyCourseRow; onClose: () => void }) {
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between mb-4">
          <h3 className="text-lg font-bold text-gray-900">Course Details</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
        </div>
        <div className="space-y-1.5 text-sm text-gray-700 mb-4">
          <p><span className="font-semibold">Course Number:</span> {row.course_number}</p>
          <p><span className="font-semibold">Course Title:</span> {row.course_title}</p>
          <p><span className="font-semibold">Credit:</span> {row.credit_structure} ({row.credits})</p>
          <p><span className="font-semibold">Course Type:</span> {row.category ? (COURSE_CATEGORY_LABELS[row.category] ?? row.category) : "—"}</p>
          <p><span className="font-semibold">Credit Type:</span> {row.credit_type ? (CREDIT_TYPE_LABELS[row.credit_type] ?? row.credit_type) : "—"}</p>
          <p><span className="font-semibold">Semester:</span> {row.semester_name ?? "—"} — {row.academic_year ?? "—"}</p>
        </div>
        <p className="text-sm font-semibold text-gray-700 mb-2">Course Teachers</p>
        <div className="space-y-1.5">
          {row.teachers.length === 0 ? (
            <p className="text-sm text-gray-500">No teachers assigned yet.</p>
          ) : row.teachers.map((t, i) => (
            <div key={i} className="flex items-center px-3 py-1.5 bg-gray-50 rounded-lg text-sm">
              <span className="w-6 text-gray-500">{i + 1}.</span><span>{t}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function EnrollmentPage() {
  const role = useRole();
  const user = useUser();
  const qc = useQueryClient();
  const isStudent = role === "student";
  const isFacultyOrAdmin = !isStudent;
  const [selectedOffering, setSelectedOffering] = useState("");
  const [statusFilter, setStatusFilter] = useState("pending");
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

  // My Courses task (this revision) — Academic Year + Semester filters, so a
  // student can view historical semesters, not just an undifferentiated flat
  // list. "" (both) = All (every semester the student has ever had courses
  // in), matching the confirmed K.7 spec's semester selector.
  const [myCalendarId, setMyCalendarId] = useState("");
  const [mySemesterId, setMySemesterId] = useState("");
  const [detailsRow, setDetailsRow] = useState<MyCourseRow | null>(null);

  const { data: myCalendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
    enabled: isStudent,
  });
  const { data: mySemesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-my-courses", myCalendarId],
    queryFn: async () => (await api.get(`/academic/calendars/${myCalendarId}/semesters`)).data,
    enabled: isStudent && !!myCalendarId,
  });

  // This is now the real "My Courses" module (BUSINESS_LOGIC.md K.7) — course
  // selection/submission itself still happens exclusively on Course
  // Registration; this page displays the RESULT of that process (view-only),
  // per K.7's explicit "Data classification: view-only" rule. The backend
  // `POST /enrollment` legacy self-enroll endpoint remains untouched — this
  // row still reflects courses enrolled via either path (legacy self-enroll
  // or Course Registration), since both write the same
  // `ams_student_enrollments` table.
  const { data: myCourses = [], isLoading: myLoading } = useQuery<MyCourseRow[]>({
    queryKey: ["my-courses", myCalendarId, mySemesterId],
    queryFn: async () => (await api.get("/enrollment/my", {
      params: { ...(myCalendarId ? { calendar_id: myCalendarId } : {}), ...(mySemesterId ? { semester_id: mySemesterId } : {}) },
    })).data,
    enabled: isStudent,
  });

  const downloadCard = useMutation({
    mutationFn: async (registrationId: string) => {
      const res = await api.get(`/enrollment/registrations/${registrationId}/document`, { responseType: "blob" });
      const blobUrl = window.URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = `RegistrationCard-${user?.student_roll ?? registrationId}.pdf`;
      document.body.appendChild(link); link.click(); link.remove();
      window.URL.revokeObjectURL(blobUrl);
    },
    onSuccess: () => toast.success("Registration Card downloaded."),
    onError: async (e: unknown) => {
      const err = e as { response?: { data?: Blob } };
      let message = "Failed to generate Registration Card.";
      if (err.response?.data instanceof Blob) {
        try { const parsed = JSON.parse(await err.response.data.text()); if (parsed?.detail) message = parsed.detail; } catch { /* non-JSON */ }
      }
      toast.error(message);
    },
  });

  const { data: offeringsAll = [] } = useQuery<Offering[]>({
    queryKey: ["ams-offerings-all"],
    queryFn: async () => (await api.get("/courses/offerings/all")).data,
    enabled: isFacultyOrAdmin,
  });

  const { data: enrollments = [] } = useQuery<EnrollmentRow[]>({
    queryKey: ["offering-enrollments", selectedOffering, statusFilter],
    queryFn: async () => (await api.get(`/enrollment/offering/${selectedOffering}?status=${statusFilter}`)).data,
    enabled: isFacultyOrAdmin && !!selectedOffering,
  });

  const process = useMutation({
    mutationFn: ({ id, status, remarks }: { id: string; status: string; remarks?: string }) =>
      api.patch(`/enrollment/${id}?status=${status}${remarks ? `&remarks=${encodeURIComponent(remarks)}` : ""}`),
    onSuccess: () => { toast.success("Updated."); qc.invalidateQueries({ queryKey: ["offering-enrollments", selectedOffering, statusFilter] }); },
  });

  const bulkApprove = useMutation({
    mutationFn: () => api.post(`/enrollment/offering/${selectedOffering}/bulk-approve`, {
      enrollment_ids: enrollments.map((e) => e.id),
      status: "approved",
    }),
    onSuccess: () => { toast.success("Bulk approved!"); qc.invalidateQueries({ queryKey: ["offering-enrollments", selectedOffering, statusFilter] }); },
  });

  // Student view — this IS the "My Courses" module now (BUSINESS_LOGIC.md
  // K.7): view-only, Academic Year + Semester filters, the confirmed column
  // set, View Details, and Registration Card access. Course selection itself
  // still happens exclusively on Course Registration — see the pointer below.
  if (isStudent) {
    // Group by (calendar_id, semester_id) so a semester with a registration
    // gets exactly one "Registration Card" action, not one per course row.
    const registrationBySemester = new Map<string, string>();
    for (const row of myCourses) {
      if (row.registration_id && row.semester_id && !registrationBySemester.has(row.semester_id)) {
        registrationBySemester.set(row.semester_id, row.registration_id);
      }
    }
    const cardEligibleStages = ["card_pending", "major_advisor_pending", "hod_pending", "hod_approved"];

    return (
      <div className="p-6 w-full">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardList size={24} className="text-[#0D6E6E]" />My Courses</h1>
        <p className="text-gray-700 text-sm mb-6">Courses selected/approved for your semester(s).</p>

        <Link href="/course-registration"
          className="flex items-center justify-between gap-4 bg-teal-50 border border-teal-200 rounded-2xl p-4 mb-6 hover:bg-teal-100 transition-colors">
          <p className="text-sm text-teal-800">
            To browse courses across all Departments and register for new ones, use <span className="font-semibold">Course Registration</span>.
          </p>
          <span className="shrink-0 flex items-center gap-1.5 px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold">
            Go to Course Registration <ArrowRight size={14} />
          </span>
        </Link>

        <div className="flex flex-wrap gap-3 mb-5">
          <select value={myCalendarId} onChange={(e) => { setMyCalendarId(e.target.value); setMySemesterId(""); }}
            className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
            <option value="">All Academic Years</option>
            {myCalendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
          </select>
          <select value={mySemesterId} onChange={(e) => setMySemesterId(e.target.value)} disabled={!myCalendarId}
            className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none disabled:opacity-50">
            <option value="">All Semesters</option>
            {mySemesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          {mySemesterId && registrationBySemester.has(mySemesterId) && (
            <button onClick={() => downloadCard.mutate(registrationBySemester.get(mySemesterId)!)} disabled={downloadCard.isPending}
              className="flex items-center gap-1.5 px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl text-sm font-semibold hover:bg-[#E6F4F4] disabled:opacity-60">
              {downloadCard.isPending ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />} View Registration Card
            </button>
          )}
        </div>

        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
          {myLoading ? (
            <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
          ) : myCourses.length === 0 ? (
            <div className="text-center py-16 text-gray-600"><ClipboardList size={40} className="mx-auto mb-3 opacity-30" /><p>No courses found for this filter.</p></div>
          ) : (
            <table className="w-full text-sm min-w-[900px]">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["SL No", "Course Type", "Course Number", "Course Title", "Credit", "Credit Type", "Status", "Action"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {myCourses.map((row, i) => (
                  <tr key={row.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                    <td className="px-4 py-3 text-gray-600">{row.category ? (COURSE_CATEGORY_LABELS[row.category] ?? row.category) : "—"}</td>
                    <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E] whitespace-nowrap">{row.course_number}</td>
                    <td className="px-4 py-3">{row.course_title}</td>
                    <td className="px-4 py-3 font-mono">{row.credit_structure} ({row.credits})</td>
                    <td className="px-4 py-3 text-gray-600">{row.credit_type ? (CREDIT_TYPE_LABELS[row.credit_type] ?? row.credit_type) : "—"}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${REG_STATUS_COLOR[row.status_label] ?? "bg-gray-100"}`}>{row.status_label}</span>
                      {row.withdrawal_request && (
                        <p className="text-xs text-amber-600 mt-0.5">Withdrawal: {row.withdrawal_request.status_label}</p>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <button onClick={() => setDetailsRow(row)} className="flex items-center gap-1 text-xs font-semibold text-[#0D6E6E] hover:underline"><Eye size={12} /> View Details</button>
                        {row.registration_id && row.registration_stage && cardEligibleStages.includes(row.registration_stage) && (
                          <button onClick={() => downloadCard.mutate(row.registration_id!)} disabled={downloadCard.isPending}
                            className="flex items-center gap-1 text-xs font-semibold text-[#0D6E6E] hover:underline">
                            <Download size={12} /> Registration Card
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

        {detailsRow && <CourseDetailsModal row={detailsRow} onClose={() => setDetailsRow(null)} />}
      </div>
    );
  }


  // Faculty/Admin view
  return (
    <div className="p-6 w-full">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardList size={24} className="text-[#0D6E6E]" />Enrollment Management</h1>
      <p className="text-gray-700 text-sm mb-6">Review and approve student enrollment requests</p>

      <div className="flex flex-wrap gap-3 mb-5">
        <select value={selectedOffering} onChange={(e) => setSelectedOffering(e.target.value)}
          className="flex-1 border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">Select a course offering…</option>
          {offeringsAll.map((o) => <option key={o.id} value={o.id}>{o.course_number} — {o.course_title} {o.section ? `(${o.section})` : ""}</option>)}
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          {["pending","approved","rejected","withdrawn"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        {selectedOffering && statusFilter === "pending" && (
          <button onClick={() => setConfirm({
            action: () => bulkApprove.mutate(),
            title: "Bulk Approve",
            message: `Are you sure you want to approve all ${enrollments.length} pending enrollment request(s)?`,
            confirmLabel: "Yes, Approve All",
            confirmClassName: "bg-green-600 hover:bg-green-700 text-white",
          })} disabled={bulkApprove.isPending || enrollments.length === 0}
            className="px-4 py-2.5 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
            {bulkApprove.isPending ? "Approving…" : `Approve All (${enrollments.length})`}
          </button>
        )}
      </div>

      {selectedOffering ? (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
          {enrollments.length === 0 ? (
            <div className="text-center py-16 text-gray-600"><ClipboardList size={40} className="mx-auto mb-3 opacity-30" /><p>No {statusFilter} enrollments.</p></div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["Student", "Roll No.", "Enrolled", "Status", "Remarks", "Actions"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {enrollments.map((e, i) => (
                  <tr key={e.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 font-medium">{e.student_name}</td>
                    <td className="px-4 py-3 font-mono text-sm">{e.student_roll || "—"}</td>
                    <td className="px-4 py-3 text-gray-700 text-sm">{new Date(e.enrolled_at).toLocaleDateString("en-IN")}</td>
                    <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[e.status] ?? ""}`}>{e.status}</span></td>
                    <td className="px-4 py-3 text-gray-700 text-sm max-w-[150px] truncate">{e.remarks ?? "—"}</td>
                    <td className="px-4 py-3">
                      {e.status === "pending" && (
                        <div className="flex gap-2">
                          <button onClick={() => setConfirm({
                            action: () => process.mutate({ id: e.id, status: "approved" }),
                            title: "Approve Enrollment",
                            message: `Are you sure you want to approve ${e.student_name}'s enrollment request?`,
                            confirmLabel: "Yes, Approve",
                            confirmClassName: "bg-green-600 hover:bg-green-700 text-white",
                          })} className="p-1.5 text-green-600 hover:bg-green-50 rounded-lg"><CheckCircle2 size={16} /></button>
                          <button onClick={() => setConfirm({
                            action: () => process.mutate({ id: e.id, status: "rejected", remarks: "Rejected by faculty." }),
                            title: "Reject Enrollment",
                            message: `Are you sure you want to reject ${e.student_name}'s enrollment request?`,
                            confirmLabel: "Yes, Reject",
                            confirmClassName: "bg-red-600 hover:bg-red-700 text-white",
                          })} className="p-1.5 text-red-500 hover:bg-red-50 rounded-lg"><XCircle size={16} /></button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="text-center py-16 text-gray-600 bg-white rounded-2xl border border-gray-200">
          <ClipboardList size={40} className="mx-auto mb-3 opacity-30" />
          <p>Select a course offering to manage enrollments.</p>
        </div>
      )}
      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
