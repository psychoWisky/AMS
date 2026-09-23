"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { GraduationCap, Loader2, Plus, Eye, X } from "lucide-react";
import {
  ApprovalHistoryTable, ProgressReportStatusBadge, RevertNotice, StudentInfoCard,
  apiErrorMessage, formatDate, type ProgressReportDetail,
} from "@/components/ui/progress-report-parts";

// The student's own Progress Report page. One report per (academic year, semester) — the
// backend enforces this with a database constraint; a reverted report is resubmitted in
// place (never a new row), so this page always opens the SAME record after a revert too.

interface ReportRow { id: string; session_year: string | null; session_semester: string | null; academic_year: string | null; status: string; status_label: string }
interface Calendar { id: string; academic_year: string }
interface Semester { id: string; name: string }

const EMPTY_FORM = {
  period_from: "", period_to: "", semester_completed: "",
  total_courses: "", total_credits_programme: "", current_semester_courses: "", current_semester_credits: "",
  courses_completed_till_date: "", credits_completed_till_date: "",
  research_title: "", research_progress: "", leave_availed: "", fellowship_stipend: "",
  expected_completion: "" as "" | "Yes" | "No", completion_delay_reason: "",
};
type Form = typeof EMPTY_FORM;
const NUMERIC_FIELDS: (keyof Form)[] = [
  "semester_completed", "total_courses", "total_credits_programme", "current_semester_courses",
  "current_semester_credits", "courses_completed_till_date", "credits_completed_till_date",
];

export default function ProgressReportPage() {
  const qc = useQueryClient();
  const [openId, setOpenId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [hydratedFor, setHydratedFor] = useState<string | null>(null);

  const { data: mine = [], isLoading } = useQuery<ReportRow[]>({
    queryKey: ["ams-progress-reports-mine"],
    queryFn: async () => (await api.get("/progress-reports/mine")).data,
  });
  const { data: detail, refetch } = useQuery<ProgressReportDetail>({
    queryKey: ["ams-progress-report-detail", openId],
    queryFn: async () => (await api.get(`/progress-reports/${openId}`)).data,
    enabled: !!openId,
  });
  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"], queryFn: async () => (await api.get("/academic/calendars")).data, enabled: showCreate,
  });
  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-progress-report-semesters", calendarId],
    queryFn: async () => (await api.get(`/academic/calendars/${calendarId}/semesters`)).data,
    enabled: showCreate && !!calendarId,
  });

  if (detail && hydratedFor !== detail.id) {
    setHydratedFor(detail.id);
    setForm({
      period_from: detail.period_from ?? "", period_to: detail.period_to ?? "",
      semester_completed: detail.semester_completed != null ? String(detail.semester_completed) : "",
      total_courses: detail.total_courses != null ? String(detail.total_courses) : "",
      total_credits_programme: detail.total_credits_programme != null ? String(detail.total_credits_programme) : "",
      current_semester_courses: detail.current_semester_courses != null ? String(detail.current_semester_courses) : "",
      current_semester_credits: detail.current_semester_credits != null ? String(detail.current_semester_credits) : "",
      courses_completed_till_date: detail.courses_completed_till_date != null ? String(detail.courses_completed_till_date) : "",
      credits_completed_till_date: detail.credits_completed_till_date != null ? String(detail.credits_completed_till_date) : "",
      research_title: detail.research_title ?? "", research_progress: detail.research_progress ?? "",
      leave_availed: detail.leave_availed ?? "", fellowship_stipend: detail.fellowship_stipend ?? "",
      expected_completion: detail.expected_completion ?? "", completion_delay_reason: detail.completion_delay_reason ?? "",
    });
  }

  function numericBody(f: Form) {
    const body: Record<string, string | number | null> = {};
    (Object.keys(f) as (keyof Form)[]).forEach((k) => {
      if (k === "expected_completion" || k === "completion_delay_reason") return;
      const v = f[k];
      if (!v.trim()) return;
      body[k] = NUMERIC_FIELDS.includes(k) ? Number(v) : v;
    });
    // The reason is only ever meaningful (and only ever sent) alongside "No" — sending it while
    // "Yes" is selected would just be ignored/cleared server-side anyway (see progress_report.py's
    // `_apply_completion_fields`), so it is never sent from here in that case either.
    if (f.expected_completion === "Yes" || f.expected_completion === "No") {
      body.expected_completion = f.expected_completion;
      if (f.expected_completion === "No") body.completion_delay_reason = f.completion_delay_reason;
    }
    return body;
  }

  const create = useMutation({
    mutationFn: () => api.post("/progress-reports", { academic_year_id: calendarId, semester_id: semesterId }),
    onSuccess: (res) => {
      toast.success("Progress Report draft created.");
      qc.invalidateQueries({ queryKey: ["ams-progress-reports-mine"] });
      setShowCreate(false); setCalendarId(""); setSemesterId("");
      setOpenId(res.data.id);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not create your Progress Report.")),
  });

  const save = useMutation({
    mutationFn: () => api.patch(`/progress-reports/${openId}`, numericBody(form)),
    onSuccess: () => { toast.success("Changes saved."); refetch(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save changes.")),
  });

  const submit = useMutation({
    mutationFn: () => api.post(`/progress-reports/${openId}/submit`),
    onSuccess: () => { toast.success("Progress Report submitted for approval."); refetch(); qc.invalidateQueries({ queryKey: ["ams-progress-reports-mine"] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not submit your Progress Report.")),
  });

  function close() { setOpenId(null); setForm(EMPTY_FORM); setHydratedFor(null); }
  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  function setExpectedCompletion(e: React.ChangeEvent<HTMLSelectElement>) {
    const value = e.target.value as "" | "Yes" | "No";
    // Selecting "Yes" clears any previously-entered reason immediately in the UI too — it
    // mirrors the backend's own "a reason is never active while Yes is selected" rule, so the
    // form never shows a stale reason the server would silently drop anyway.
    setForm((f) => ({ ...f, expected_completion: value, completion_delay_reason: value === "Yes" ? "" : f.completion_delay_reason }));
  }

  const input = (label: string, key: keyof Form, type = "text", value?: string) => (
    <div>
      <label className="block text-sm font-semibold text-gray-700 mb-1">{label}</label>
      {detail?.can_edit ? (
        <input type={type} value={form[key]} onChange={set(key)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
      ) : <p className="text-gray-800 text-sm py-2">{value ?? (type === "date" ? formatDate(detail?.[key as keyof ProgressReportDetail] as string) : (detail?.[key as keyof ProgressReportDetail] as string) || "—")}</p>}
    </div>
  );
  const textarea = (label: string, key: keyof Form) => (
    <div className="md:col-span-2">
      <label className="block text-sm font-semibold text-gray-700 mb-1">{label}</label>
      {detail?.can_edit ? (
        <textarea rows={3} value={form[key]} onChange={set(key)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
      ) : <p className="text-gray-800 text-sm py-2 whitespace-pre-wrap">{(detail?.[key as keyof ProgressReportDetail] as string) || "—"}</p>}
    </div>
  );

  return (
    <div className="p-6 w-full max-w-4xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><GraduationCap size={24} className="text-[#0D6E6E]" />Progress Report</h1>
          <p className="text-gray-700 text-base mt-1">Create and track your semester Progress Reports.</p>
        </div>
        <button onClick={() => setShowCreate(true)} className="flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#178F8F]">
          <Plus size={16} /> Create Progress Report
        </button>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
        {isLoading ? (
          <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
        ) : mine.length === 0 ? (
          <div className="text-center py-16 text-gray-500">You have not created a Progress Report yet.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["SL No.", "Session Year", "Session Semester", "Academic Year", "Status", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {mine.map((r, i) => (
                <tr key={r.id} className="border-b border-gray-50 last:border-0">
                  <td className="px-4 py-2.5">{i + 1}</td>
                  <td className="px-4 py-2.5">{r.session_year ?? "—"}</td>
                  <td className="px-4 py-2.5">{r.session_semester ?? "—"}</td>
                  <td className="px-4 py-2.5">{r.academic_year ?? "—"}</td>
                  <td className="px-4 py-2.5"><ProgressReportStatusBadge status={r.status} label={r.status_label} /></td>
                  <td className="px-4 py-2.5"><button onClick={() => setOpenId(r.id)} className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]"><Eye size={13} /> View Details</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showCreate && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setShowCreate(false)}>
          <div className="bg-white rounded-2xl max-w-md w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-gray-900">New Progress Report</h3>
              <button onClick={() => setShowCreate(false)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <p className="text-sm text-gray-600">Please fill up the required details to create semester progress report.</p>
            <div>
              <label className="block text-sm font-semibold text-gray-700 mb-1">Academic Year</label>
              <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); }} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                <option value="">Select…</option>
                {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-sm font-semibold text-gray-700 mb-1">Semester</label>
              <select value={semesterId} onChange={(e) => setSemesterId(e.target.value)} disabled={!calendarId} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm disabled:opacity-50">
                <option value="">Select…</option>
                {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </div>
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowCreate(false)} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
              <button onClick={() => create.mutate()} disabled={!calendarId || !semesterId || create.isPending}
                className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                {create.isPending ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}

      {openId && detail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={close}>
          <div className="bg-white rounded-2xl max-w-3xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-gray-900">Progress Report</h2>
              <div className="flex items-center gap-3">
                <ProgressReportStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
              </div>
            </div>

            {detail.revert_info && <RevertNotice info={detail.revert_info} />}
            <StudentInfoCard r={detail} />

            <section className="space-y-3">
              <h3 className="text-base font-bold text-gray-900">Semester Information</h3>
              <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-3">
                <p className="text-sm"><span className="font-semibold text-gray-700">Academic Year:</span> {detail.academic_year ?? "—"}</p>
                <p className="text-sm"><span className="font-semibold text-gray-700">Semester:</span> {detail.semester_name ?? "—"}</p>
                <p className="text-sm"><span className="font-semibold text-gray-700">Session Year:</span> {detail.session_year ?? "—"}</p>
                <p className="text-sm"><span className="font-semibold text-gray-700">Session Semester:</span> {detail.session_semester ?? "—"}</p>
                {input("Period From", "period_from", "date")}
                {input("Period To", "period_to", "date")}
                {input("Semester Completed", "semester_completed", "number")}
              </div>
            </section>

            <section className="space-y-3">
              <h3 className="text-base font-bold text-gray-900">Academic Progress</h3>
              <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-3">
                {input("Total No. of Courses", "total_courses", "number")}
                {input("Total Credits (Whole Programme)", "total_credits_programme", "number")}
                {input("No. of Current Semester Courses", "current_semester_courses", "number")}
                {input("Credits Completed in Current Semester", "current_semester_credits", "number")}
                {input("No. of Courses Completed Till Date", "courses_completed_till_date", "number")}
                {input("No. of Credits Completed Till Date", "credits_completed_till_date", "number")}
              </div>
            </section>

            <section className="space-y-3">
              <h3 className="text-base font-bold text-gray-900">Research</h3>
              <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 gap-3">
                {textarea("Title of Research Problem", "research_title")}
                {textarea("Progress of Research Till Date", "research_progress")}
              </div>
            </section>

            <section className="space-y-3">
              <h3 className="text-base font-bold text-gray-900">Other</h3>
              <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 gap-3">
                {textarea("Nature of Leave Availed Till Date", "leave_availed")}
                {textarea("Nature of Fellowship/Stipend Availed", "fellowship_stipend")}
              </div>
            </section>

            <section className="space-y-3">
              <h3 className="text-base font-bold text-gray-900">Completion Expectation</h3>
              <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Whether the courses as well as research works are expected to be completed in time:{detail.can_edit && " *"}</label>
                  {detail.can_edit ? (
                    <select value={form.expected_completion} onChange={setExpectedCompletion} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                      <option value="">Select…</option>
                      <option value="Yes">Yes</option>
                      <option value="No">No</option>
                    </select>
                  ) : <p className="text-gray-800 text-sm py-2">{detail.expected_completion ?? "—"}</p>}
                </div>
                {(detail.can_edit ? form.expected_completion === "No" : detail.expected_completion === "No") && (
                  <div>
                    <label className="block text-sm font-semibold text-gray-700 mb-1">If not, explain the reason:{detail.can_edit && " *"}</label>
                    {detail.can_edit ? (
                      <textarea rows={3} value={form.completion_delay_reason} onChange={set("completion_delay_reason")}
                        className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                    ) : <p className="text-gray-800 text-sm py-2 whitespace-pre-wrap">{detail.completion_delay_reason || "—"}</p>}
                  </div>
                )}
              </div>
            </section>


            <section className="space-y-2">
              <h3 className="text-base font-bold text-gray-900">Approval History</h3>
              <ApprovalHistoryTable history={detail.history} />
            </section>

            {detail.can_edit && (
              <div className="flex gap-3 justify-end border-t border-gray-100 pt-4">
                <button onClick={close} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Close</button>
                <button onClick={() => save.mutate()} disabled={save.isPending}
                  className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                  {save.isPending ? "Saving…" : "Save Changes"}
                </button>
                <button onClick={() => { if (confirm("Submit this Progress Report for approval? You will not be able to edit it until it is reverted.")) submit.mutate(); }}
                  disabled={submit.isPending}
                  className="px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
                  {submit.isPending ? "Submitting…" : "Submit"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
