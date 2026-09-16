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
  // Selected-Courses-before-first-registration fix (this revision): the
  // fallback `/enrollment/my` source (see `myEnrollmentsForSemester` below)
  // only reports `status`/`status_label` for a withdrawal request — the
  // extra detail fields are optional so that fallback shape satisfies this
  // interface too; every render in THIS page only ever reads `status`/
  // `status_label` off an item's `withdrawal_request`.
  status: string; status_label: string;
  id?: string; reason?: string; decision_remark?: string | null;
  requested_at?: string; decided_at?: string | null;
}
interface EnrollmentItem {
  id: string; offering_id: string; course_number: string; course_title: string; credits: number;
  status: string; status_label: string; remarks: string | null; withdrawal_request: WithdrawalRequestInfo | null;
  // Registration Card Preview task (this revision) — additive, optional
  // (the pre-registration `/enrollment/my` fallback below doesn't set
  // them): grouping/instructor detail so the on-screen preview can mirror
  // the printed card's course table without depending on the PDF pipeline.
  category?: string | null; credit_structure?: string; credit_type?: string | null; instructors?: string[];
  // Major/Minor/Supporting discipline task (this revision) — this
  // selection's classification (if any) and the OFFERING's own department
  // (Course Registration is offering-based — see `_enroll_dict`'s
  // docstring), used to derive which departments remain legal for the
  // NEXT Minor/Supporting pick (see `useMinorSupportingState` below).
  classification?: string | null; department_id?: string | null; department_name?: string | null;
}
// Minimal shape consumed from the pre-existing, semester-scoped `/enrollment/my`
// endpoint (unchanged — already used by "My Courses") — reused here only as a
// fallback data source, never a second/duplicated calculation.
interface MyEnrollmentItem {
  id: string; offering_id: string; course_number: string; course_title: string; credits: number;
  status: string; status_label: string; remarks: string | null;
  classification?: string | null; department_id?: string | null; department_name?: string | null;
  withdrawal_request: { status: string; status_label: string } | null;
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
  // Registration Card Preview task (this revision) — additive display
  // fields, mirroring exactly what the PDF already shows (see
  // `_registration_dict`'s docstring), so an on-page preview never needs
  // the fragile Chromium/PDF pipeline to show the same information.
  program_name: string | null; program_level: string | null; department_name: string | null;
  semester_name: string | null; academic_year: string | null;
  major_advisor_name: string | null; hod_name: string | null; student_mobile: string | null;
  // Major/Minor/Supporting discipline task (this revision) — backend-
  // derived (see `_registration_dict`'s docstring), never client text.
  major_discipline_name: string | null; minor_discipline_name: string | null;
  supporting_discipline_name: string | null;
}

// Registration Card Preview task (this revision) — mirrors the PDF's own
// `_CATEGORY_LABELS` (enrollment.py) exactly, so the on-screen grouping
// headings read identically to the printed card.
const CATEGORY_LABELS: Record<string, string> = {
  optional: "Optional Course", core: "Core Course", compulsory: "Compulsory Course (CC)",
  research: "Research Course", seminar: "Seminar Course", deficiency: "Deficiency",
  bridge: "Bridge", prerequisite: "Prerequisite", mandatory_mba: "Mandatory Course (MBA)",
  uncategorized: "Other Course(s)",
};
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
  // Student Classification task (this revision) — this is an IDENTIFIER the
  // student assigns to the course(s) they are about to select ("I am
  // selecting this as a Major course"), never a filter on `Course.category`
  // (a separate, pre-existing, unrelated concept — see the Offering
  // interface's own `category` field, untouched by this control). Defaults
  // to "major" (the first of exactly four allowed values — Major/Minor/
  // Supporting/Compulsory; Research/Seminar are PPW-only classifications
  // and are deliberately not offered here). Every course checked in ONE
  // batch shares this classification; the backend independently
  // re-validates every selection regardless of this value (see
  // `register_courses`'s classification validation) — this control can
  // never bypass authorization.
  const [classification, setClassification] = useState("major");
  const [courseSearch, setCourseSearch] = useState("");
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

  // Selected-Courses-before-first-registration fix (this revision): a
  // `CourseRegistration` row for this semester is only ever created the
  // FIRST time this student calls `POST /enrollment/register` for it — a
  // student whose only courses so far are active (pending/approved) legacy
  // enrollments (no `registration_id`, predating this workflow) has NO
  // `CourseRegistration` row yet, so `currentRegistration` above is
  // `undefined` even though real selected courses already exist. Falling
  // back to the pre-existing, semester-scoped `GET /enrollment/my` endpoint
  // (already used by "My Courses", unchanged) fixes both the "Selected
  // Courses shows nothing / 0 credits" display bug AND makes those courses
  // correctly excluded from "Available Courses" below from the very first
  // page load — not only after the student adds a new course and a
  // registration gets created incidentally. Only fetched while there is no
  // `currentRegistration` yet, since once one exists its own `items` are
  // already this same semester-scoped population (never a second,
  // independently-computed source once a registration exists).
  const { data: myEnrollmentsForSemester = [] } = useQuery<MyEnrollmentItem[]>({
    queryKey: ["ams-my-enrollments-for-registration", semesterId],
    queryFn: async () => (await api.get("/enrollment/my", { params: { semester_id: semesterId } })).data,
    enabled: !!semesterId && !currentRegistration,
  });

  // Registration Card task: the binary "submitted -> read-only-forever" view
  // is gone. A registration is still fully editable (add/remove/withdraw)
  // while `is_editable` is true — only once the student has explicitly
  // submitted the Registration Card does the selection UI disappear. The
  // backend is authoritative for this — `is_editable` comes straight from
  // `CourseRegistration.stage`, never derived/guessed on the frontend.
  // No `currentRegistration` at all is never "locked" — a legacy-only
  // student with no registration yet can always withdraw/request-withdrawal
  // on those courses (the backend's existing unrestricted legacy-item path).
  const isLocked = currentRegistration ? !currentRegistration.is_editable : false;

  const activeItems: EnrollmentItem[] = currentRegistration?.items ?? myEnrollmentsForSemester
    .filter((e) => e.status !== "withdrawn")
    .map((e) => ({
      id: e.id, offering_id: e.offering_id, course_number: e.course_number, course_title: e.course_title,
      credits: e.credits, status: e.status, status_label: e.status_label, remarks: e.remarks,
      withdrawal_request: e.withdrawal_request,
      classification: e.classification, department_id: e.department_id, department_name: e.department_name,
    }));
  // Selected-Courses/credit fix (this revision): prefer the backend's
  // authoritative `selected_credits` (semester-scoped, includes legacy
  // registration_id=NULL rows) over a client-side sum. Falling back to
  // summing `activeItems` when there is no registration yet at all (which
  // now correctly includes the pre-registration legacy items above too) —
  // never a second, independently-computed total that could drift from the
  // backend's once a registration exists.
  const usedCredits = currentRegistration?.selected_credits ?? activeItems.reduce((sum, it) => sum + (it.credits || 0), 0);
  const remainingCredits = MAX_SEMESTER_CREDITS - usedCredits;
  const registeredOfferingIds = new Set(activeItems.map((it) => it.offering_id));
  const baseAvailableOfferings = offerings.filter((o) => !registeredOfferingIds.has(o.id));

  // Student Classification task (this revision) — three INDEPENDENT
  // dimensions, composed together (AND), mirroring PPW's identical pattern
  // (see ppw/page.tsx and the backend's GET /ppw/available-courses
  // docstring): (1) classification-aware department narrowing — which
  // department(s) are LEGAL for the chosen classification, per the approved
  // Major/Minor/Supporting rules; (2) the student's own, separately-chosen
  // Department filter; (3) free-text search. None of this ever touches
  // `Course.category` — a course whose category is e.g. "research" remains
  // fully visible/selectable under any classification, exactly as required
  // (the student, not `Course.category`, decides how a course is being
  // used). All of this is UX only, never the authorization boundary —
  // `register_courses` independently re-validates every selection's
  // department regardless of what this client-side filter shows.
  const lpmDepartmentId = departments.find((d) => d.code === "LPM")?.id;
  const existingMinorDeptId = activeItems.find((it) => it.classification === "minor")?.department_id ?? null;
  const existingSupportingDeptIds = activeItems
    .filter((it) => it.classification === "supporting")
    .map((it) => it.department_id)
    .filter((id): id is string => !!id);
  function classificationAllowsDepartment(o: Offering): boolean {
    if (classification === "major") return o.department_id === user?.department_id;
    if (classification === "minor") {
      if (existingMinorDeptId) return o.department_id === existingMinorDeptId;
      return o.department_id !== user?.department_id;
    }
    if (classification === "supporting") {
      if (existingSupportingDeptIds.length === 0) return !!lpmDepartmentId && o.department_id === lpmDepartmentId;
      const isLpm = !!lpmDepartmentId && o.department_id === lpmDepartmentId;
      if (isLpm) return true;
      return o.department_id !== user?.department_id && o.department_id !== existingMinorDeptId;
    }
    return true; // compulsory — no department restriction invented; never Course.category-based.
  }
  const availableOfferings = baseAvailableOfferings.filter((o) => {
    if (!classificationAllowsDepartment(o)) return false;
    // Department filter (independent dimension — Part D): a plain,
    // student-chosen AND-filter, composed with the classification's own
    // narrowing above rather than disabled/mutually-exclusive with it — a
    // student may legitimately combine "Major Courses" + "Department: VETM"
    // in one query, exactly as specified.
    if (departmentFilter && o.department_id !== departmentFilter) return false;
    const q = courseSearch.trim().toLowerCase();
    if (q && !o.course_number.toLowerCase().includes(q) && !o.course_title.toLowerCase().includes(q)) return false;
    return true;
  });
  const selectedCredits = Array.from(selected).reduce((sum, id) => {
    const o = availableOfferings.find((x) => x.id === id);
    return sum + (o?.credits || 0);
  }, 0);
  const allApproved = activeItems.length > 0 && activeItems.every((it) => it.status === "approved");

  // Registration Card Preview task (this revision) — groups `activeItems`
  // by `category`, exactly mirroring the printed card's own grouping
  // (`_build_registration_card_context`'s `classifications`), so the
  // on-screen preview always matches what the PDF would show for the same
  // data — never a second, independently-invented grouping.
  const previewGroups = activeItems.reduce<Record<string, EnrollmentItem[]>>((acc, it) => {
    const key = it.category ?? "uncategorized";
    (acc[key] ??= []).push(it);
    return acc;
  }, {});

  function invalidateAll() {
    qc.invalidateQueries({ queryKey: ["ams-my-registrations"] });
    qc.invalidateQueries({ queryKey: ["ams-eligible-offerings"] });
  }

  const submitRegistration = useMutation({
    mutationFn: () => api.post("/enrollment/register", {
      calendar_id: calendarId, semester_id: semesterId, offering_ids: Array.from(selected),
      // Major/Minor/Supporting discipline task (this revision) — every
      // offering in this batch shares the currently-selected classification
      // (omitted entirely when unclassified, preserving the exact
      // pre-existing request shape for that case).
      ...(classification ? { classifications: Object.fromEntries(Array.from(selected).map((id) => [id, classification])) } : {}),
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
      </div>

      {classification === "major" && !user?.department_id && (
        <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2 mb-4">
          Your department is not configured, so no Major courses can be shown. Contact administration.
        </p>
      )}

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

          {(currentRegistration || activeItems.length > 0) && (
            <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
              <div className="flex items-center justify-between mb-3">
                <h2 className="font-bold text-gray-800">Your Selected Courses</h2>
                {/* Selected-Courses-before-first-registration fix: before any
                    CourseRegistration row exists yet, there is no backend
                    `stage`/`status_label` to show — derive an equivalent
                    label straight from the (already semester-scoped) items
                    instead of hiding the badge outright. */}
                <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold ${STAGE_STYLE[currentRegistration?.stage ?? ""] ?? "bg-amber-100 text-amber-700"}`}>
                  {currentRegistration?.status_label ?? (allApproved ? "Course Teacher Approved" : "Course Teacher Approval Pending")}
                </span>
              </div>
              {currentRegistration?.revert_remark && (
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
                  persists (in a different shape) after submission. Requires
                  an actual `currentRegistration` (never shown for the
                  pre-registration legacy-only fallback above) since
                  submit/download both need a real CourseRegistration id —
                  a student must add at least one course via this page
                  (creating that row) before a card can be submitted. */}
              {currentRegistration && allApproved && !isLocked && (
                <div className="mt-4 bg-teal-50 border border-teal-200 rounded-xl p-4">
                  <p className="text-sm font-semibold text-teal-800 mb-3">All selected courses are approved.</p>
                  <div className="flex gap-2">
                    <button onClick={() => downloadCard.mutate()} disabled={downloadCard.isPending}
                      className="flex items-center gap-1.5 px-4 py-2 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl text-sm font-semibold hover:bg-[#E6F4F4] disabled:opacity-60">
                      {/* Registration Card Preview task (this revision): relabeled from
                          "View Registration Card" — the Registration Card Preview
                          section below now covers "viewing" the content on-screen;
                          this button's only job is producing the official PDF file,
                          matching PPW's own "Download PPW Document" naming. */}
                      {downloadCard.isPending ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />} {downloadCard.isPending ? "Generating…" : "Download PDF"}
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
                  <p className="text-sm text-blue-700 mb-3">Status: {currentRegistration?.status_label}. No further edits are possible for this registration.</p>
                  <button onClick={() => downloadCard.mutate()} disabled={downloadCard.isPending}
                    className="flex items-center gap-1.5 px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-bold hover:bg-[#178F8F] disabled:opacity-60">
                    {downloadCard.isPending ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />} Download PDF
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Available Courses (Section order + Student Classification tasks,
              this revision) — moved to come BEFORE the Registration Card
              Preview below (was: Selected -> Preview -> Available; now:
              Selected -> Available -> Preview), and its Classification/
              Department/Search controls now live here, scoped to exactly
              what they affect — they never touch "Your Selected Courses" or
              the Preview below. Still visible/actionable even after a first
              submission, as long as the registration is still editable
              (this task's core fix, unchanged). */}
          {!isLocked && (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden mb-6">
              <div className="px-4 py-3 border-b border-gray-100 space-y-3">
                <h2 className="font-bold text-gray-800">Available Courses</h2>
                <div className="flex flex-wrap gap-3">
                  <div>
                    <label className="block text-xs font-semibold text-gray-500 mb-1">Classification</label>
                    {/* Student Classification (this revision) — an IDENTIFIER
                        of how the student is using the course they select
                        ("I am selecting this as a Major course"), never a
                        filter on `Course.category`. Exactly four values —
                        Research/Seminar are PPW-only classifications and are
                        deliberately not offered here (Part O). Changing this
                        narrows which DEPARTMENT(S) are legal per the
                        approved Major/Minor/Supporting rules (see
                        `classificationAllowsDepartment` above) — it never
                        filters by `Course.category`, so a course whose
                        category is e.g. "research" remains fully visible/
                        selectable here. */}
                    <select value={classification} onChange={(e) => { setClassification(e.target.value); setSelected(new Set()); }}
                      className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
                      <option value="major">Major Courses</option>
                      <option value="minor">Minor Courses</option>
                      <option value="supporting">Supporting Courses</option>
                      <option value="compulsory">Compulsory Credit Courses</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-gray-500 mb-1">Department</label>
                    {/* Department filter — an INDEPENDENT dimension (Part D):
                        composed together with Classification (AND), never
                        disabled/mutually-exclusive with it. "Classification:
                        Major Courses" + "Department: VETM" is a valid,
                        supported combination. */}
                    <select value={departmentFilter} onChange={(e) => { setDepartmentFilter(e.target.value); setSelected(new Set()); }}
                      className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
                      <option value="">All Departments</option>
                      {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                    </select>
                  </div>
                  <div className="flex-1 min-w-[200px]">
                    <label className="block text-xs font-semibold text-gray-500 mb-1">Search</label>
                    <input value={courseSearch} onChange={(e) => setCourseSearch(e.target.value)} placeholder="Search courses…"
                      className="w-full border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none" />
                  </div>
                </div>
              </div>
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
                      : departmentFilter || courseSearch
                        ? "No eligible courses match the current Classification/Department/Search filters."
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

          {/* Registration Card Preview task (this revision) — an always-
              available, on-screen mock-up of the printed card, mirroring
              PPW's own "PPW Preview" section (ppw/page.tsx): plain React/
              HTML, entirely independent of the Chromium/PDF pipeline, so a
              student can always see exactly what the card contains even if
              PDF generation is temporarily unavailable. Requires an actual
              `currentRegistration` (its `program_name`/`department_name`/
              `major_advisor_name`/etc. fields only exist once one does) —
              same gating as the Download/Submit banner above. Moved to come
              AFTER Available Courses (Section order task, this revision). */}
          {currentRegistration && (
            <div className="bg-white rounded-2xl border border-gray-200 p-8 mb-6">
              <h2 className="font-bold text-gray-800 mb-4">Registration Card Preview</h2>
              <div className="border border-gray-300 rounded-xl p-8 max-w-3xl mx-auto text-sm leading-relaxed">
                <div className="text-center mb-4">
                  <p className="font-bold text-base underline">ASSAM VETERINARY AND FISHERY UNIVERSITY</p>
                  <p>Faculty : Faculty of Veterinary Science</p>
                  <p>College : {currentRegistration.department_name ?? "—"}</p>
                </div>
                <p className="text-center font-bold text-base underline my-4">SEMESTER COURSE REGISTRATION CARD</p>

                <table className="w-full my-4 text-sm border border-gray-400">
                  <tbody>
                    <tr>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold w-1/4">Name</td>
                      <td className="border border-gray-300 px-2 py-1.5 w-1/4">{user?.full_name ?? "—"}</td>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold w-1/4">Roll No.</td>
                      <td className="border border-gray-300 px-2 py-1.5 w-1/4">{user?.student_roll ?? "—"}</td>
                    </tr>
                    <tr>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold">Phone No.</td>
                      <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.student_mobile ?? "—"}</td>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold">Semester</td>
                      <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.semester_name ?? "—"}</td>
                    </tr>
                    <tr>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold">Academic Session</td>
                      <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.academic_year ?? "—"}</td>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold">Degree Programme</td>
                      <td className="border border-gray-300 px-2 py-1.5">
                        {currentRegistration.program_name ?? "—"}{currentRegistration.program_level ? ` (${currentRegistration.program_level})` : ""}
                      </td>
                    </tr>
                    <tr>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold">Department</td>
                      <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.department_name ?? "—"}</td>
                      <td className="border border-gray-300 px-2 py-1.5 font-semibold">Major Advisor</td>
                      <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.major_advisor_name ?? "—"}</td>
                    </tr>
                    {/* Major/Minor/Supporting discipline task (this
                        revision) — backend-derived (see
                        `_registration_dict`'s docstring); rows only render
                        when at least one classified selection exists, so a
                        registration with none renders exactly as before. */}
                    {(currentRegistration.major_discipline_name || currentRegistration.minor_discipline_name || currentRegistration.supporting_discipline_name) && (
                      <>
                        <tr>
                          <td className="border border-gray-300 px-2 py-1.5 font-semibold">Major Discipline</td>
                          <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.major_discipline_name ?? "—"}</td>
                          <td className="border border-gray-300 px-2 py-1.5 font-semibold">Minor Discipline</td>
                          <td className="border border-gray-300 px-2 py-1.5">{currentRegistration.minor_discipline_name ?? "—"}</td>
                        </tr>
                        <tr>
                          <td className="border border-gray-300 px-2 py-1.5 font-semibold">Supporting Discipline</td>
                          <td className="border border-gray-300 px-2 py-1.5" colSpan={3}>{currentRegistration.supporting_discipline_name ?? "—"}</td>
                        </tr>
                      </>
                    )}
                  </tbody>
                </table>

                <p className="font-bold mt-4 mb-2">Selected Courses</p>
                {Object.keys(previewGroups).length === 0 ? (
                  <p className="text-gray-500 italic text-sm">No courses selected</p>
                ) : (
                  Object.entries(previewGroups).map(([cat, rows]) => (
                    <table key={cat} className="w-full text-xs border border-gray-400 mb-3">
                      <thead>
                        <tr className="bg-gray-100">
                          <td colSpan={6} className="border border-gray-300 px-2 py-1 font-bold">
                            {(CATEGORY_LABELS[cat] ?? cat.replace(/_/g, " ")).toUpperCase()}
                          </td>
                        </tr>
                        <tr className="bg-gray-50">
                          {["SL No", "Course Title", "Course No.", "Credit Hrs.", "Nature", "Course Instructor"].map((h) => (
                            <th key={h} className="border border-gray-300 px-2 py-1 text-left">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row, idx) => (
                          <tr key={row.id}>
                            <td className="border border-gray-300 px-2 py-1">{idx + 1}</td>
                            <td className="border border-gray-300 px-2 py-1">{row.course_title}</td>
                            <td className="border border-gray-300 px-2 py-1 font-mono">{row.course_number}</td>
                            <td className="border border-gray-300 px-2 py-1">{row.credits}{row.credit_structure ? `(${row.credit_structure})` : ""}</td>
                            <td className="border border-gray-300 px-2 py-1">{row.credit_type === "non_credit" ? "Non-Credit" : "Credit"}</td>
                            <td className="border border-gray-300 px-2 py-1">{row.instructors?.join(", ") || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ))
                )}
                <p className="text-right font-bold text-sm">Total Credits: {usedCredits}</p>

                <p className="text-sm mt-4">Status: <span className="font-bold">{currentRegistration.status_label}</span></p>

                <div className="grid grid-cols-3 gap-6 mt-10 text-center text-xs">
                  <div className="border-t border-gray-400 pt-2"><p className="font-semibold">Signature of Student</p></div>
                  <div className="border-t border-gray-400 pt-2">
                    <p className="font-semibold">Signature of Major Advisor</p>
                    {currentRegistration.major_advisor_name && <p className="text-gray-500 mt-0.5">{currentRegistration.major_advisor_name}</p>}
                  </div>
                  <div className="border-t border-gray-400 pt-2">
                    <p className="font-semibold">Signature of Head of the Dept.</p>
                    {currentRegistration.hod_name && <p className="text-gray-500 mt-0.5">{currentRegistration.hod_name}</p>}
                  </div>
                </div>
              </div>
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
