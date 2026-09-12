"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ClipboardCheck, Loader2, CheckSquare, Square, X, Download, FileText } from "lucide-react";
import { CREDIT_TYPE_LABELS } from "@/lib/utils";

interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface DepartmentOpt { id: string; name: string; code: string; }
interface Offering {
  id: string; course_number: string; course_title: string; credit_structure: string; credits: number;
  category: string | null; credit_type: string | null; is_research: boolean;
  semester_name: string | null; department_id: string | null; department_name: string | null; faculty_names: string[];
  status: string; enrolled_count: number; max_enrollment: number;
}
interface WithdrawalRequestInfo {
  id: string; status: string; status_label: string; reason: string; decision_remark: string | null;
  requested_at: string; decided_at: string | null;
}
interface EnrollmentItem {
  id: string; offering_id: string; course_number: string; course_title: string; credits: number;
  status: string; status_label: string; remarks: string | null; withdrawal_request: WithdrawalRequestInfo | null;
}
interface Registration {
  id: string; semester_id: string; calendar_id: string; stage: string; status_label: string;
  is_editable: boolean; is_card_ready: boolean;
  revert_remark: string | null; submitted_at: string; card_submitted_at: string | null;
  // Selected-Courses fix (this revision): `items` is now semester-scoped —
  // it includes ALL of the student's active (pending/approved) enrollments
  // for this semester, including legacy rows with no `registration_id`, not
  // only rows linked to this specific registration. `selected_credits` is
  // the backend-authoritative total over that same population — always
  // prefer this over summing `items` client-side (see `usedCredits` below).
  selected_credits: number; max_credits: number;
  items: EnrollmentItem[]; withdrawn_items: EnrollmentItem[];
}

const STAGE_STYLE: Record<string, string> = {
  teacher_pending: "bg-amber-100 text-amber-700",
  card_pending: "bg-teal-100 text-teal-700",
  major_advisor_pending: "bg-blue-100 text-blue-700",
  hod_pending: "bg-purple-100 text-purple-700",
  hod_approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
};
const ITEM_STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-100 text-amber-700",
  approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
  withdrawn: "bg-gray-100 text-gray-600",
};
const MAX_SEMESTER_CREDITS = 20;

function WithdrawalReasonModal({ onSubmit, onCancel, isPending }: { onSubmit: (reason: string) => void; onCancel: () => void; isPending: boolean }) {
  const [reason, setReason] = useState("");
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
        <h3 className="text-lg font-bold mb-2">Request Withdrawal</h3>
        <p className="text-sm text-gray-600 mb-3">This course has already been approved by its Course Teacher. Provide a reason — the Course Teacher will approve or reject your request.</p>
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} placeholder="Reason for withdrawal…"
          className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] resize-none" />
        <div className="flex gap-3 mt-4">
          <button onClick={onCancel} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-sm font-medium">Cancel</button>
          <button onClick={() => { if (!reason.trim()) { toast.error("A reason is required to request withdrawal."); return; } onSubmit(reason.trim()); }}
            disabled={isPending}
            className="flex-1 py-2.5 bg-red-600 text-white rounded-xl text-sm font-bold hover:bg-red-700 disabled:opacity-60">
            {isPending ? "Submitting…" : "Submit Request"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function CourseRegistrationPage() {
  const user = useUser();
  const qc = useQueryClient();
  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  // Course-visibility change — a student's own department no longer limits
  // this catalogue; this is purely an optional narrowing filter across ALL
  // departments' courses. "" = All Departments (the default).
  const [departmentFilter, setDepartmentFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmSubmit, setConfirmSubmit] = useState(false);
  const [confirmCard, setConfirmCard] = useState(false);
  const [withdrawConfirm, setWithdrawConfirm] = useState<EnrollmentItem | null>(null);
  const [withdrawalRequestFor, setWithdrawalRequestFor] = useState<EnrollmentItem | null>(null);

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-registration", calendarId],
    queryFn: async () => (await api.get(`/academic/calendars/${calendarId}/semesters`)).data,
    enabled: !!calendarId,
  });

  // Department filter master data — the existing Department master-data
  // endpoint, not a new concept.
  const { data: departments = [] } = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
  });

  const { data: offerings = [], isLoading: offeringsLoading } = useQuery<Offering[]>({
    queryKey: ["ams-eligible-offerings", calendarId, semesterId, departmentFilter],
    queryFn: async () => (await api.get("/courses/offerings/all", {
      params: {
        calendar_id: calendarId, semester_id: semesterId,
        ...(departmentFilter ? { department_id: departmentFilter } : {}),
      },
    })).data,
    enabled: !!calendarId && !!semesterId,
  });

  const { data: myRegistrations = [], isLoading: regsLoading } = useQuery<Registration[]>({
    queryKey: ["ams-my-registrations"],
    queryFn: async () => (await api.get("/enrollment/registrations")).data,
    enabled: !!user,
  });

  const currentRegistration = myRegistrations.find((r) => r.semester_id === semesterId);
  // Registration Card task: the binary "submitted -> read-only-forever" view
  // is gone. A registration is still fully editable (add/remove/withdraw)
  // while `is_editable` is true — only once the student has explicitly
  // submitted the Registration Card does the selection UI disappear. The
  // backend is authoritative for this — `is_editable` comes straight from
  // `CourseRegistration.stage`, never derived/guessed on the frontend.
  const isLocked = currentRegistration ? !currentRegistration.is_editable : false;

  const activeItems = currentRegistration?.items ?? [];
  // Selected-Courses/credit fix (this revision): prefer the backend's
  // authoritative `selected_credits` (semester-scoped, includes legacy
  // registration_id=NULL rows) over a client-side sum. Falling back to
  // summing `activeItems` only when there is no registration yet at all
  // (nothing selected, so both are equivalently 0) — never a second,
  // independently-computed total that could drift from the backend's.
  const usedCredits = currentRegistration?.selected_credits ?? activeItems.reduce((sum, it) => sum + (it.credits || 0), 0);
  const remainingCredits = MAX_SEMESTER_CREDITS - usedCredits;
  const registeredOfferingIds = new Set(activeItems.map((it) => it.offering_id));
  const availableOfferings = offerings.filter((o) => !registeredOfferingIds.has(o.id));
  const selectedCredits = Array.from(selected).reduce((sum, id) => {
    const o = availableOfferings.find((x) => x.id === id);
    return sum + (o?.credits || 0);
  }, 0);
  const allApproved = activeItems.length > 0 && activeItems.every((it) => it.status === "approved");

  function invalidateAll() {
    qc.invalidateQueries({ queryKey: ["ams-my-registrations"] });
    qc.invalidateQueries({ queryKey: ["ams-eligible-offerings"] });
  }

  const submitRegistration = useMutation({
    mutationFn: () => api.post("/enrollment/register", {
      calendar_id: calendarId, semester_id: semesterId, offering_ids: Array.from(selected),
    }),
    onSuccess: () => { toast.success("Courses added for approval."); setSelected(new Set()); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to submit registration."),
  });

  const withdrawPending = useMutation({
    mutationFn: (enrollmentId: string) => api.delete(`/enrollment/${enrollmentId}`),
    onSuccess: () => { toast.success("Course withdrawn."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to withdraw course."),
  });

  const requestWithdrawal = useMutation({
    mutationFn: ({ enrollmentId, reason }: { enrollmentId: string; reason: string }) =>
      api.post(`/enrollment/${enrollmentId}/withdrawal-request`, { reason }),
    onSuccess: () => { toast.success("Withdrawal request submitted."); invalidateAll(); setWithdrawalRequestFor(null); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to submit withdrawal request."),
  });

  const submitCard = useMutation({
    mutationFn: () => api.post(`/enrollment/registrations/${currentRegistration?.id}/submit`),
    onSuccess: () => { toast.success("Registration Card submitted."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to submit Registration Card."),
  });

  const downloadCard = useMutation({
    mutationFn: async () => {
      const res = await api.get(`/enrollment/registrations/${currentRegistration?.id}/document`, { responseType: "blob" });
      const blobUrl = window.URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = `RegistrationCard-${user?.student_roll ?? currentRegistration?.id}.pdf`;
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

  function toggle(id: string, credits: number) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) { next.delete(id); return next; }
      if (usedCredits + selectedCredits + credits > MAX_SEMESTER_CREDITS) {
        toast.error(`Adding this course would exceed the maximum of ${MAX_SEMESTER_CREDITS} credits for this semester.`);
        return next;
      }
      next.add(id);
      return next;
    });
  }

  return (
    <div className="p-6 w-full">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Course Registration</h1>
      <p className="text-gray-700 text-sm mb-6">Select and submit your courses for approval by your Course Teacher, Major Advisor, and HOD.</p>

      <div className="flex flex-wrap gap-3 mb-5">
        <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); setSelected(new Set()); }}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">Select Academic Year…</option>
          {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
        </select>
        <select value={semesterId} onChange={(e) => { setSemesterId(e.target.value); setSelected(new Set()); }} disabled={!calendarId}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none disabled:opacity-50">
          <option value="">Select Semester…</option>
          {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        {/* Department filter (this task's confirmed requirement) — a
            student's OWN department no longer restricts this catalogue; this
            is purely an optional narrowing filter across ALL departments. */}
        <select value={departmentFilter} onChange={(e) => { setDepartmentFilter(e.target.value); setSelected(new Set()); }}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Departments</option>
          {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </div>

      {semesterId && (regsLoading ? (
        <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
      ) : (
        <>
          {/* Credit summary — informational only; the backend independently
              re-validates every addition regardless of what this shows. */}
          <div className="bg-white rounded-2xl border border-gray-200 p-4 mb-5 flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-600">Selected Credits</p>
              <p className="text-2xl font-bold text-gray-900">{usedCredits} <span className="text-base font-normal text-gray-500">/ {MAX_SEMESTER_CREDITS}</span></p>
            </div>
            <div className="text-right">
              <p className="text-sm text-gray-600">Remaining</p>
              <p className={`text-2xl font-bold ${remainingCredits > 0 ? "text-[#0D6E6E]" : "text-gray-400"}`}>{Math.max(0, remainingCredits)}</p>
            </div>
          </div>

          {currentRegistration && (
            <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
              <div className="flex items-center justify-between mb-3">
                <h2 className="font-bold text-gray-800">Your Selected Courses</h2>
                <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold ${STAGE_STYLE[currentRegistration.stage] ?? "bg-gray-100"}`}>{currentRegistration.status_label}</span>
              </div>
              {currentRegistration.revert_remark && (
                <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-800 mb-3">
                  <span className="font-bold">Reverted — reason: </span>{currentRegistration.revert_remark}
                </div>
              )}
              <div className="space-y-2">
                {activeItems.map((it) => (
                  <div key={it.id} className="flex items-center justify-between p-2.5 bg-gray-50 rounded-lg text-sm">
                    <div>
                      <span className="font-mono font-bold text-[#0D6E6E]">{it.course_number}</span> <span>{it.course_title}</span>
                      <span className="text-gray-500"> ({it.credits} cr)</span>
                      {it.remarks && <p className="text-xs text-red-600 mt-0.5">{it.remarks}</p>}
                      {it.withdrawal_request && (
                        <p className={`text-xs mt-0.5 ${it.withdrawal_request.status === "rejected" ? "text-red-600" : it.withdrawal_request.status === "approved" ? "text-gray-500" : "text-amber-600"}`}>
                          Withdrawal Request: {it.withdrawal_request.status_label}
                        </p>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${ITEM_STATUS_STYLE[it.status] ?? "bg-gray-100"}`}>{it.status_label}</span>
                      {!isLocked && it.status === "pending" && (
                        <button onClick={() => setWithdrawConfirm(it)} className="text-xs font-semibold text-red-600 hover:underline">Withdraw</button>
                      )}
                      {!isLocked && it.status === "approved" && !it.withdrawal_request && (
                        <button onClick={() => setWithdrawalRequestFor(it)} className="text-xs font-semibold text-red-600 hover:underline">Request Withdrawal</button>
                      )}
                      {!isLocked && it.status === "approved" && it.withdrawal_request?.status === "rejected" && (
                        <button onClick={() => setWithdrawalRequestFor(it)} className="text-xs font-semibold text-red-600 hover:underline">Request Again</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              {/* Registration Card banner — appears once every currently-
                  selected course has been Course-Teacher approved, and
                  persists (in a different shape) after submission. */}
              {allApproved && !isLocked && (
                <div className="mt-4 bg-teal-50 border border-teal-200 rounded-xl p-4">
                  <p className="text-sm font-semibold text-teal-800 mb-3">All selected courses are approved.</p>
                  <div className="flex gap-2">
                    <button onClick={() => downloadCard.mutate()} disabled={downloadCard.isPending}
                      className="flex items-center gap-1.5 px-4 py-2 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl text-sm font-semibold hover:bg-[#E6F4F4] disabled:opacity-60">
                      {downloadCard.isPending ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />} View Registration Card
                    </button>
                    <button onClick={() => setConfirmCard(true)} disabled={submitCard.isPending}
                      className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-bold hover:bg-[#178F8F] disabled:opacity-60">
                      {submitCard.isPending ? "Submitting…" : "Submit Registration Card"}
                    </button>
                  </div>
                </div>
              )}
              {isLocked && (
                <div className="mt-4 bg-blue-50 border border-blue-200 rounded-xl p-4">
                  <p className="text-sm font-semibold text-blue-800 mb-1">Registration Card Submitted</p>
                  <p className="text-sm text-blue-700 mb-3">Status: {currentRegistration.status_label}. No further edits are possible for this registration.</p>
                  <button onClick={() => downloadCard.mutate()} disabled={downloadCard.isPending}
                    className="flex items-center gap-1.5 px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-bold hover:bg-[#178F8F] disabled:opacity-60">
                    {downloadCard.isPending ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />} Download PDF
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Available courses — remains visible and actionable even after a
              first submission, as long as the registration is still editable
              (this task's core fix). */}
          {!isLocked && (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100"><h2 className="font-bold text-gray-800">Available Courses</h2></div>
              {/* Table scrolls within its own bounded area (both axes) so the
                  horizontal scrollbar stays reachable without scrolling the
                  whole page down, and many rows scroll internally instead of
                  growing the page — title/footer above/below stay fixed. */}
              <div className="overflow-auto max-h-[65vh]">
              {offeringsLoading ? (
                <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
              ) : availableOfferings.length === 0 ? (
                <div className="text-center py-16 text-gray-600">
                  <ClipboardCheck size={40} className="mx-auto mb-3 opacity-30" />
                  <p>
                    {offerings.length > 0 && availableOfferings.length === 0
                      ? "You have already selected every eligible course for this semester."
                      : departmentFilter
                        ? "No eligible courses found for this department in this semester."
                        : "No eligible courses found for this semester. If this seems wrong, your academic program may not be configured — contact administration."}
                  </p>
                </div>
              ) : (
                <table className="w-full text-sm min-w-[900px]">
                  <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
                    <tr>{["", "Course Number", "Course Title", "Department", "Credit", "Credit Type", "Course Teachers"].map((h) => (
                      <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {availableOfferings.map((o, i) => {
                      const wouldExceed = !selected.has(o.id) && usedCredits + selectedCredits + o.credits > MAX_SEMESTER_CREDITS;
                      return (
                        <tr key={o.id} onClick={() => !wouldExceed && toggle(o.id, o.credits)}
                          className={`${wouldExceed ? "opacity-40 cursor-not-allowed" : "cursor-pointer"} ${i % 2 === 0 ? "bg-white" : "bg-gray-50/50"} hover:bg-[#E6F4F4]`}
                          title={wouldExceed ? `Exceeds the ${MAX_SEMESTER_CREDITS}-credit semester limit` : undefined}>
                          <td className="px-4 py-3">{selected.has(o.id) ? <CheckSquare size={18} className="text-[#0D6E6E]" /> : <Square size={18} className="text-gray-400" />}</td>
                          <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E] whitespace-nowrap">{o.course_number}</td>
                          <td className="px-4 py-3">{o.course_title}</td>
                          <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{o.department_name ?? "—"}</td>
                          <td className="px-4 py-3 font-mono">{o.credit_structure} ({o.credits})</td>
                          <td className="px-4 py-3 text-gray-600">{o.credit_type ? CREDIT_TYPE_LABELS[o.credit_type] : "—"}</td>
                          <td className="px-4 py-3 text-gray-600">{o.faculty_names.join(", ") || "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
              </div>
              {availableOfferings.length > 0 && (
                <div className="flex justify-end p-4 border-t border-gray-100">
                  <button onClick={() => setConfirmSubmit(true)} disabled={selected.size === 0 || submitRegistration.isPending}
                    className="px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50">
                    {currentRegistration ? "Add Selected Course" : "Submit Registration"}{selected.size !== 1 ? "s" : ""} ({selected.size})
                  </button>
                </div>
              )}
            </div>
          )}
        </>
      ))}

      {confirmSubmit && (
        <ConfirmDialog
          title={currentRegistration ? "Add Courses" : "Submit Course Registration"}
          message={`${currentRegistration ? "Add" : "Submit"} ${selected.size} course(s) for Course Teacher approval?`}
          confirmLabel="Yes, Continue"
          confirmClassName="bg-[#0D6E6E] hover:bg-[#178F8F] text-white"
          onCancel={() => setConfirmSubmit(false)}
          onConfirm={() => { submitRegistration.mutate(); setConfirmSubmit(false); }}
        />
      )}
      {confirmCard && (
        <ConfirmDialog
          title="Submit Registration Card"
          message="Once submitted, you will no longer be able to add, remove, or withdraw courses for this registration. It will be sent to your Major Advisor for approval. Continue?"
          confirmLabel="Yes, Submit"
          confirmClassName="bg-[#0D6E6E] hover:bg-[#178F8F] text-white"
          onCancel={() => setConfirmCard(false)}
          onConfirm={() => { submitCard.mutate(); setConfirmCard(false); }}
        />
      )}
      {withdrawConfirm && (
        <ConfirmDialog
          title="Withdraw Course"
          message={`Withdraw ${withdrawConfirm.course_number} — ${withdrawConfirm.course_title}? This course is still pending Course Teacher approval.`}
          confirmLabel="Yes, Withdraw"
          confirmClassName="bg-red-600 hover:bg-red-700 text-white"
          onCancel={() => setWithdrawConfirm(null)}
          onConfirm={() => { withdrawPending.mutate(withdrawConfirm.id); setWithdrawConfirm(null); }}
        />
      )}
      {withdrawalRequestFor && (
        <WithdrawalReasonModal
          isPending={requestWithdrawal.isPending}
          onCancel={() => setWithdrawalRequestFor(null)}
          onSubmit={(reason) => requestWithdrawal.mutate({ enrollmentId: withdrawalRequestFor.id, reason })}
        />
      )}
    </div>
  );
}
