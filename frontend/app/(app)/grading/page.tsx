"use client";
import { Suspense, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeft, BarChart3, CheckCircle2, Eye, FileText, Loader2, Pencil, Plus, RotateCcw, Save, Send, Users } from "lucide-react";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { GradesheetStructureModal } from "@/components/ui/gradesheet-modal";
import {
  ApprovalHistory, AttendanceBadge, GRADESHEET_TYPE_LABELS, GradesheetStatusBadge, PdfPreviewModal, SignatoryPanel,
  apiErrorMessage, formatDateTime,
  type ApprovalCycle, type AssignedCourse, type GradesheetDetail, type OfferingSheets, type SheetRow,
} from "@/components/ui/gradesheet-parts";

// Gradesheet: instructors manage their assigned courses' gradesheets (create -> enter marks and
// attendance -> submit/sign); HOD / Incharge Academic Cell / DPGS / Controller of Examination
// review, approve (= sign) or revert. Every action is authorized by the backend; the buttons
// shown here only mirror the `permissions` object the backend returns.

interface Calendar { id: string; name: string; academic_year: string }
interface Semester { id: string; calendar_id: string; name: string }

const th = "text-left px-4 py-3 font-semibold text-gray-700";

function Loading() {
  return <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;
}

export default function GradingPage() {
  return <Suspense fallback={<Loading />}><GradingRouter /></Suspense>;
}

function GradingRouter() {
  const params = useSearchParams();
  const router = useRouter();
  const role = useRole();
  const sheetId = params.get("sheet");
  const offeringId = params.get("offering");
  if (sheetId) return <SheetDetailView sheetId={sheetId} />;
  if (offeringId && role === "faculty") return <ManageOffering offeringId={offeringId} onBack={() => router.push("/grading")} />;
  return <GradingHome role={role} />;
}

// ── Home: assigned courses (instructor) + approver inbox ─────────────────────

function GradingHome({ role }: { role: string | null }) {
  const isInstructor = role === "faculty";
  const isCoe = role === "controller_of_examination";
  const [tab, setTab] = useState<"pending" | "finalized">("pending");
  return (
    <div className="p-6 w-full">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><BarChart3 size={24} className="text-[#0D6E6E]" />{isInstructor ? "Course Gradesheet Management" : "Gradesheet Approvals"}</h1>
        <p className="text-gray-700 text-base mt-1">{isInstructor ? "Gradesheets for the courses assigned to you" : "Course-wise gradesheets awaiting your review, approval (signature) or revert"}</p>
      </div>
      {isInstructor && <AssignedCourses />}
      {isCoe && (
        <div className="flex gap-2 mb-4">
          {(["pending", "finalized"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)} className={`px-4 py-2 rounded-xl text-base font-semibold border ${tab === t ? "bg-[#0D6E6E] text-white border-[#0D6E6E]" : "bg-white text-gray-700 border-gray-200 hover:bg-gray-50"}`}>
              {t === "pending" ? "Awaiting my approval" : "Finalized gradesheets"}
            </button>
          ))}
        </div>
      )}
      <InboxPanel scope={isCoe ? tab : "pending"} heading={isInstructor ? "Awaiting your approval as a course instructor" : undefined} />
    </div>
  );
}

function AssignedCourses() {
  const router = useRouter();
  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const { data: calendars = [] } = useQuery<Calendar[]>({ queryKey: ["ams-calendars"], queryFn: async () => (await api.get("/academic/calendars")).data });
  const { data: allSemesters = [] } = useQuery<Semester[]>({ queryKey: ["ams-semesters-all"], queryFn: async () => (await api.get("/academic/semesters")).data });
  const semesters = allSemesters.filter((s) => !calendarId || s.calendar_id === calendarId);
  const { data: rows = [], isLoading, isError } = useQuery<AssignedCourse[]>({
    queryKey: ["gs-assigned", calendarId, semesterId],
    queryFn: async () => (await api.get("/grading/assigned-courses", { params: { ...(calendarId ? { calendar_id: calendarId } : {}), ...(semesterId ? { semester_id: semesterId } : {}) } })).data,
  });
  return (
    <div className="mb-8">
      <div className="flex flex-wrap gap-3 mb-4">
        <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); }} className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Academic Years</option>
          {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
        </select>
        <select value={semesterId} onChange={(e) => setSemesterId(e.target.value)} className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Semesters</option>
          {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </div>
      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[60vh]">
        {isLoading ? <Loading /> : isError ? (
          <div className="text-center py-12 text-red-600">Could not load your assigned courses.</div>
        ) : rows.length === 0 ? (
          <div className="text-center py-14 text-gray-600"><BarChart3 size={40} className="mx-auto mb-3 opacity-30" /><p>No courses are assigned to you for the selected period.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
              {["SL NO", "College/Degree", "Department", "Course Number", "Course Title", "Course Credit", "Action"].map((h) => <th key={h} className={th}>{h}</th>)}
            </tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.offering_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-3">{r.college_degree ?? "—"}</td>
                  <td className="px-4 py-3">{r.department ?? "—"}</td>
                  <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E]">{r.course_number}</td>
                  <td className="px-4 py-3">{r.course_title}<span className="block text-xs text-gray-500">{r.semester} · {r.academic_year}</span></td>
                  <td className="px-4 py-3 font-mono">{r.course_credit}</td>
                  <td className="px-4 py-3">
                    <button onClick={() => router.push(`/grading?offering=${r.offering_id}`)} className="px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">Manage Gradesheet</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function InboxPanel({ scope, heading }: { scope: "pending" | "finalized"; heading?: string }) {
  const router = useRouter();
  const { data: rows = [], isLoading, isError } = useQuery<SheetRow[]>({
    queryKey: ["gs-inbox", scope],
    queryFn: async () => (await api.get("/grading/inbox", { params: { scope } })).data,
  });
  return (
    <div>
      {heading && <h2 className="text-lg font-bold text-gray-900 mb-3">{heading}</h2>}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[60vh]">
        {isLoading ? <Loading /> : isError ? (
          <div className="text-center py-12 text-red-600">Could not load gradesheets.</div>
        ) : rows.length === 0 ? (
          <div className="text-center py-12 text-gray-600"><FileText size={36} className="mx-auto mb-3 opacity-30" /><p>{scope === "finalized" ? "No finalized gradesheets yet." : "Nothing is awaiting your action."}</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
              {["SL NO", "Course", "Department", "Semester", "Type", "Teacher", "Submitted", "Status", "Action"].map((h) => <th key={h} className={th}>{h}</th>)}
            </tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-3"><span className="font-mono font-bold text-[#0D6E6E]">{r.course_number}</span><span className="block">{r.course_title}</span></td>
                  <td className="px-4 py-3">{r.department ?? "—"}</td>
                  <td className="px-4 py-3">{r.semester ?? "—"}<span className="block text-xs text-gray-500">{r.academic_year}</span></td>
                  <td className="px-4 py-3">{GRADESHEET_TYPE_LABELS[r.gradesheet_type] ?? r.gradesheet_type}</td>
                  <td className="px-4 py-3">{r.teacher ?? "—"}</td>
                  <td className="px-4 py-3">{formatDateTime(r.submitted_at)}</td>
                  <td className="px-4 py-3"><GradesheetStatusBadge status={r.status} /></td>
                  <td className="px-4 py-3">
                    <button onClick={() => router.push(`/grading?sheet=${r.id}`)} className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]"><Eye size={13} /> View Details</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── Manage Gradesheet (one offering) ─────────────────────────────────────────

function ManageOffering({ offeringId, onBack }: { offeringId: string; onBack: () => void }) {
  const router = useRouter();
  const [showCreate, setShowCreate] = useState(false);
  const { data, isLoading, isError, error } = useQuery<OfferingSheets>({
    queryKey: ["gs-offering", offeringId],
    queryFn: async () => (await api.get(`/grading/offering/${offeringId}/sheets`)).data,
    retry: false,
  });
  return (
    <div className="p-6 w-full">
      <button onClick={onBack} className="flex items-center gap-1.5 text-sm font-semibold text-[#0D6E6E] mb-4"><ArrowLeft size={16} /> Back to assigned courses</button>
      {isLoading ? <Loading /> : isError || !data ? (
        <div className="bg-white rounded-2xl border border-gray-200 py-14 text-center text-red-600">{apiErrorMessage(error, "This course could not be loaded.")}</div>
      ) : (
        <>
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
            <div>
              <h1 className="text-3xl font-bold text-gray-900">{data.offering.course_number} <span className="text-gray-700 font-semibold">({data.offering.course_title})</span></h1>
              <p className="text-gray-700 mt-1">{data.offering.semester} · {data.offering.session} · Credit {data.offering.credit}</p>
            </div>
            {data.can_create && (
              <button onClick={() => setShowCreate(true)} className="inline-flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#0a5858]"><Plus size={16} /> Generate Gradesheet</button>
            )}
          </div>
          <div className="mb-5 bg-[#E6F4F4] border border-[#0D6E6E]/30 rounded-xl px-4 py-3 flex items-center gap-2 text-[#0D6E6E] font-semibold">
            <Users size={18} /> Total {data.offering.total_students} students have registered this course
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-auto">
            {data.sheets.length === 0 ? (
              <div className="text-center py-14 text-gray-600"><FileText size={40} className="mx-auto mb-3 opacity-30" /><p>No gradesheet has been generated for this course yet.</p></div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>
                  {["SL NO", "Teacher", "Gradesheet Type", "Created At", "Status", "Action"].map((h) => <th key={h} className={th}>{h}</th>)}
                </tr></thead>
                <tbody>
                  {data.sheets.map((s, i) => (
                    <tr key={s.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                      <td className="px-4 py-3">{s.teacher ?? "—"}</td>
                      <td className="px-4 py-3">{GRADESHEET_TYPE_LABELS[s.gradesheet_type] ?? s.gradesheet_type} Gradesheet</td>
                      <td className="px-4 py-3">{formatDateTime(s.created_at)}</td>
                      <td className="px-4 py-3"><GradesheetStatusBadge status={s.status} /></td>
                      <td className="px-4 py-3"><button onClick={() => router.push(`/grading?sheet=${s.id}`)} className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]"><Eye size={13} /> View Details</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {showCreate && (
            <GradesheetStructureModal mode="create" offering={data.offering} students={data.students} sheets={data.sheets}
              onClose={() => setShowCreate(false)} onSaved={(id) => { setShowCreate(false); router.push(`/grading?sheet=${id}`); }} />
          )}
        </>
      )}
    </div>
  );
}

// ── Gradesheet details / data entry / approval ───────────────────────────────

interface Edit { marks: Record<string, string>; attendance: string; remark: string; absent: boolean }
const parseNum = (v: string): number | null => (v.trim() === "" ? null : Number(v));
function buildEdits(d: GradesheetDetail): Record<string, Edit> {
  const next: Record<string, Edit> = {};
  d.entries.forEach((e) => {
    next[e.student_id] = {
      marks: Object.fromEntries(d.structure.components.map((c) => [c.code, e.component_marks[c.code] === null || e.component_marks[c.code] === undefined ? "" : String(e.component_marks[c.code])])),
      attendance: e.attendance_percent === null ? "" : String(e.attendance_percent), remark: e.remark ?? "", absent: e.is_absent,
    };
  });
  return next;
}
const fmt = (n: number | null | undefined, d = 2) => (n === null || n === undefined ? "—" : Number.isInteger(n) ? String(n) : n.toFixed(d).replace(/\.?0+$/, ""));

function SheetDetailView({ sheetId }: { sheetId: string }) {
  const router = useRouter();
  const qc = useQueryClient();
  const role = useRole();
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [dirty, setDirty] = useState(false);
  const [showPdf, setShowPdf] = useState(false);
  const [showStructure, setShowStructure] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [confirm, setConfirm] = useState<"submit" | "approve" | null>(null);
  const [revertOpen, setRevertOpen] = useState(false);
  const [remark, setRemark] = useState("");

  const { data: d, isLoading, isError, error } = useQuery<GradesheetDetail>({
    queryKey: ["gs-detail", sheetId],
    queryFn: async () => (await api.get(`/grading/sheets/${sheetId}`)).data,
    retry: false, refetchOnWindowFocus: false,
  });
  const { data: history } = useQuery<{ cycles: ApprovalCycle[] }>({
    queryKey: ["gs-history", sheetId],
    queryFn: async () => (await api.get(`/grading/sheets/${sheetId}/approvals`)).data,
    enabled: showHistory && !!d,
  });

  // Re-seed the editable copy whenever the server sends a new version of the sheet (after a save,
  // approval, revert…). Done while rendering (guarded by identity) rather than in an effect.
  const [syncedFor, setSyncedFor] = useState<GradesheetDetail | undefined>(undefined);
  if (d && d !== syncedFor) { setSyncedFor(d); setEdits(buildEdits(d)); setDirty(false); }

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["gs-detail", sheetId] });
    qc.invalidateQueries({ queryKey: ["gs-history", sheetId] });
    qc.invalidateQueries({ queryKey: ["gs-inbox"] });
    qc.invalidateQueries({ queryKey: ["gs-offering"] });
    qc.invalidateQueries({ queryKey: ["gs-assigned"] });
  };

  const comps = d?.structure.components ?? [];
  const theory = comps.filter((c) => c.component_type === "theory");
  const practical = comps.filter((c) => c.component_type === "practical");

  const save = useMutation({
    mutationFn: () => api.put(`/grading/sheets/${sheetId}/entries`, {
      entries: d!.entries.map((e) => {
        const ed = edits[e.student_id];
        return {
          student_id: e.student_id, is_absent: ed.absent, remark: ed.remark, attendance_percent: parseNum(ed.attendance),
          component_marks: Object.fromEntries(comps.map((c) => [c.code, parseNum(ed.marks[c.code] ?? "")])),
        };
      }),
    }),
    onSuccess: () => { toast.success("Gradesheet saved."); refresh(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save the gradesheet.")),
  });
  const submit = useMutation({
    mutationFn: () => api.post(`/grading/sheets/${sheetId}/submit`),
    onSuccess: (r) => { toast.success(r.data?.message ?? "Submitted."); setConfirm(null); refresh(); },
    onError: (e) => { setConfirm(null); toast.error(apiErrorMessage(e, "Could not submit the gradesheet.")); },
  });
  const approve = useMutation({
    mutationFn: () => api.post(`/grading/sheets/${sheetId}/approve`),
    onSuccess: (r) => { toast.success(r.data?.message ?? "Approved."); setConfirm(null); refresh(); },
    onError: (e) => { setConfirm(null); toast.error(apiErrorMessage(e, "Could not approve the gradesheet.")); refresh(); },
  });
  const revert = useMutation({
    mutationFn: () => api.post(`/grading/sheets/${sheetId}/revert`, { remark }),
    onSuccess: (r) => { toast.success(r.data?.message ?? "Reverted."); setRevertOpen(false); setRemark(""); refresh(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not revert the gradesheet.")),
  });

  const setEdit = (sid: string, patch: Partial<Edit>) => { setEdits((p) => ({ ...p, [sid]: { ...p[sid], ...patch } })); setDirty(true); };
  const setMark = (sid: string, code: string, v: string) => { setEdits((p) => ({ ...p, [sid]: { ...p[sid], marks: { ...p[sid].marks, [code]: v } } })); setDirty(true); };

  const back = () => router.push(d && role === "faculty" ? `/grading?offering=${d.offering_id}` : "/grading");
  const live = useMemo(() => {
    const out: Record<string, { theory: number; practical: number; grand: number; pct: number | null }> = {};
    if (!d) return out;
    const max = comps.reduce((n, c) => n + c.max_marks, 0);
    d.entries.forEach((e) => {
      const ed = edits[e.student_id]; if (!ed) return;
      const sum = (list: typeof comps) => list.reduce((n, c) => n + (Number(ed.marks[c.code]) || 0), 0);
      const t = sum(theory), p = sum(practical);
      out[e.student_id] = { theory: t, practical: p, grand: t + p, pct: max > 0 ? Math.round(((t + p) / max) * 10000) / 100 : null };
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d, edits]);

  if (isLoading) return <div className="p-6"><Loading /></div>;
  if (isError || !d) {
    return (
      <div className="p-6"><button onClick={() => router.push("/grading")} className="flex items-center gap-1.5 text-sm font-semibold text-[#0D6E6E] mb-4"><ArrowLeft size={16} /> Back</button>
        <div className="bg-white rounded-2xl border border-gray-200 py-14 text-center text-red-600">{apiErrorMessage(error, "This gradesheet could not be loaded.")}</div></div>
    );
  }
  const p = d.permissions;
  const editable = p.can_edit_data;
  const cell = "border border-gray-300 px-2 py-2 text-center";
  const inp = "w-20 border border-gray-200 rounded-md px-2 py-1 text-center focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-100";

  return (
    <div className="p-6 w-full">
      <button onClick={back} className="flex items-center gap-1.5 text-sm font-semibold text-[#0D6E6E] mb-4"><ArrowLeft size={16} /> Back</button>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">{d.course.course_number} <span className="text-gray-700 font-semibold">({d.course.course_title})</span></h1>
          <div className="flex flex-wrap items-center gap-2 mt-2">
            <span className="px-2.5 py-1 rounded-full text-sm font-semibold bg-blue-50 text-blue-700">{GRADESHEET_TYPE_LABELS[d.gradesheet_type] ?? d.gradesheet_type} Gradesheet</span>
            <GradesheetStatusBadge status={d.status} />
            <span className="text-sm text-gray-600">Prepared by {d.teacher ?? "—"} · created {formatDateTime(d.created_at)}</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={() => setShowPdf(true)} className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl font-semibold hover:bg-[#E6F4F4]"><FileText size={16} /> View / Print PDF</button>
          {p.can_edit_structure && <button onClick={() => setShowStructure(true)} className="inline-flex items-center gap-2 px-4 py-2.5 border border-gray-200 rounded-xl font-semibold text-gray-700 hover:bg-gray-50"><Pencil size={16} /> Edit Structure</button>}
          {editable && <button onClick={() => save.mutate()} disabled={save.isPending || !dirty} className="inline-flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#0a5858] disabled:opacity-50">{save.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} Save Gradesheet</button>}
          {p.can_submit && <button onClick={() => (dirty ? toast.error("Save your changes before submitting.") : setConfirm("submit"))} className="inline-flex items-center gap-2 px-4 py-2.5 bg-indigo-600 text-white rounded-xl font-semibold hover:bg-indigo-700"><Send size={16} /> Submit for Approval</button>}
          {p.can_approve && <button onClick={() => setConfirm("approve")} className="inline-flex items-center gap-2 px-4 py-2.5 bg-green-600 text-white rounded-xl font-semibold hover:bg-green-700"><CheckCircle2 size={16} /> Approve</button>}
          {p.can_revert && <button onClick={() => setRevertOpen(true)} className="inline-flex items-center gap-2 px-4 py-2.5 bg-red-600 text-white rounded-xl font-semibold hover:bg-red-700"><RotateCcw size={16} /> Revert</button>}
        </div>
      </div>

      {p.awaiting && d.status !== "approved" && <div className="mb-4 bg-amber-50 border border-amber-200 text-amber-800 rounded-xl px-4 py-2.5 text-sm font-semibold">Currently awaiting: {p.awaiting}.{p.acting_as ? ` You are acting as ${p.acting_as}; approving records your signature.` : ""}</div>}
      {d.instructor_deadline_at && d.status === "instructor_pending" && <div className="mb-4 bg-blue-50 border border-blue-200 text-blue-800 rounded-xl px-4 py-2.5 text-sm">Other course instructors can approve until {formatDateTime(d.instructor_deadline_at)}. If they have not by then, the gradesheet moves to the HOD automatically.</div>}
      {d.status === "reverted" && <div className="mb-4 bg-red-50 border border-red-200 text-red-800 rounded-xl px-4 py-2.5 text-sm">This gradesheet was reverted. Correct it and submit it again to start a new approval cycle.</div>}

      <div className="grid grid-cols-3 gap-3 mb-5 max-w-xl">
        {[["Total Students", d.students.total], ["Male Students", d.students.male], ["Female Students", d.students.female]].map(([l, v]) => (
          <div key={String(l)} className="bg-white rounded-xl border border-gray-200 px-4 py-3"><p className="text-sm text-gray-600">{l}</p><p className="text-2xl font-bold text-[#0D6E6E]">{v}</p></div>
        ))}
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-5">
        <h2 className="text-base font-bold text-gray-900 mb-3">Course Information</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-3 text-sm">
          {([["College", d.course.college], ["Course Number", d.course.course_number], ["Course Title", d.course.course_title], ["Credit", d.course.credit],
            ["Credit Type", d.course.credit_type], ["Department", d.course.department], ["Semester", d.course.semester], ["Session", d.course.session]] as [string, string | null][]).map(([l, v]) => (
            <div key={l}><p className="text-gray-600">{l}</p><p className="font-semibold">{v ?? "—"}</p></div>
          ))}
        </div>
        <p className="text-sm text-gray-700 mt-3">
          Theory {fmt(d.structure.total_theory_marks)} (pass {fmt(d.structure.theory_pass_marks)}) · Practical {fmt(d.structure.total_practical_marks)} (pass {fmt(d.structure.practical_pass_marks)})
        </p>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-bold text-gray-900">Student Grades</h2>
          {editable && dirty && <span className="text-sm text-amber-700 font-semibold">Unsaved changes — save to recalculate grades</span>}
        </div>
        <div className="overflow-auto max-h-[70vh]">
          <table className="w-full text-sm border-collapse">
            <thead className="bg-gray-50 sticky top-0 z-10">
              <tr>
                <th className={cell} rowSpan={2}>SL NO</th><th className={cell} rowSpan={2}>Roll No</th><th className={`${cell} text-left`} rowSpan={2}>Name</th>
                {theory.length > 0 && <th className={cell} colSpan={theory.length}>Theory</th>}
                <th className={cell} colSpan={(theory.length ? 1 : 0) + practical.length + 3}>Total</th>
                <th className={cell} rowSpan={2}>Attendance %</th><th className={`${cell} text-left`} rowSpan={2}>Remark</th>
                {editable && <th className={cell} rowSpan={2}>Absent</th>}
              </tr>
              <tr>
                {theory.map((c) => <th key={c.code} className={cell}>{c.name} ({fmt(c.max_marks)})</th>)}
                {theory.length > 0 && <th className={cell}>Total Theory ({fmt(d.structure.total_theory_marks)})</th>}
                {practical.map((c) => <th key={c.code} className={cell}>{c.name} ({fmt(c.max_marks)})</th>)}
                <th className={cell}>Grand Total ({fmt(d.structure.total_theory_marks + d.structure.total_practical_marks)})</th>
                <th className={cell}>Marks %</th><th className={cell}>Grade</th>
              </tr>
            </thead>
            <tbody>
              {d.entries.map((e, i) => {
                const ed = edits[e.student_id]; const lv = live[e.student_id];
                if (!ed) return null;
                const markCell = (c: (typeof comps)[number]) => (
                  <td key={c.code} className={cell}>
                    {editable ? <input type="number" min={0} max={c.max_marks} step="0.01" value={ed.marks[c.code] ?? ""} disabled={ed.absent} onChange={(ev) => setMark(e.student_id, c.code, ev.target.value)} className={inp} />
                      : e.is_absent ? "AB" : fmt(e.component_marks[c.code])}
                  </td>
                );
                const show = (n: number | null | undefined) => (ed.absent || e.is_absent ? "AB" : fmt(n));
                return (
                  <tr key={e.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/60"}>
                    <td className={cell}>{i + 1}</td><td className={`${cell} font-mono`}>{e.student_roll ?? "—"}</td><td className={`${cell} text-left`}>{e.student_name ?? "—"}</td>
                    {theory.map(markCell)}
                    {theory.length > 0 && <td className={`${cell} font-semibold`}>{show(editable ? lv?.theory : e.theory_total)}</td>}
                    {practical.map(markCell)}
                    <td className={`${cell} font-semibold`}>{show(editable ? lv?.grand : e.grand_total)}</td>
                    <td className={cell}>{ed.absent || e.is_absent ? "—" : (editable ? lv?.pct : e.marks_percent) == null ? "—" : (editable ? lv!.pct! : e.marks_percent!).toFixed(2)}</td>
                    <td className={`${cell} font-bold ${e.grade_letter === "F" ? "text-red-600" : "text-[#0D6E6E]"}`}>{e.grade_letter ?? "—"}</td>
                    <td className={cell}>
                      {editable ? <input type="number" min={0} max={100} step="0.01" value={ed.attendance} onChange={(ev) => setEdit(e.student_id, { attendance: ev.target.value })} className={inp} />
                        : <AttendanceBadge percent={e.attendance_percent} />}
                    </td>
                    <td className={`${cell} text-left`}>
                      {editable ? <input value={ed.remark} onChange={(ev) => setEdit(e.student_id, { remark: ev.target.value })} className="w-40 border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /> : (e.remark ?? "")}
                    </td>
                    {editable && <td className={cell}><input type="checkbox" checked={ed.absent} onChange={(ev) => setEdit(e.student_id, { absent: ev.target.checked })} /></td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-gray-500 mt-2">Attendance: below 75% red · 75%–85% yellow · above 85% green. AB = absent. Grades are calculated by the server when you save.</p>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-5">
        <h2 className="text-base font-bold text-gray-900 mb-3">Signatories</h2>
        <SignatoryPanel signatories={d.signatories} />
        <button onClick={() => setShowHistory((v) => !v)} className="mt-4 text-sm font-semibold text-[#0D6E6E] hover:underline">{showHistory ? "Hide" : "Show"} approval history</button>
        {showHistory && <div className="mt-3">{history ? <ApprovalHistory cycles={history.cycles} /> : <Loading />}</div>}
      </div>

      {showPdf && <PdfPreviewModal title={`Gradesheet — ${d.course.course_number}`} path={`/grading/sheets/${sheetId}/document`} filename={`Gradesheet-${d.course.course_number}.pdf`} onClose={() => setShowPdf(false)} />}
      {showStructure && (
        <GradesheetStructureModal mode="edit" sheetId={sheetId} offering={{ id: d.offering_id, course_number: d.course.course_number, credit: d.course.credit }} initial={{ ...d.structure }}
          onClose={() => setShowStructure(false)} onSaved={() => { setShowStructure(false); refresh(); }} />
      )}
      {confirm === "submit" && <ConfirmDialog title="Submit for approval?" message="Submitting signs the gradesheet as its preparer and locks it for editing until it is approved or reverted." confirmLabel="Submit" confirmClassName="bg-indigo-600 hover:bg-indigo-700 text-white" onConfirm={() => submit.mutate()} onCancel={() => setConfirm(null)} />}
      {confirm === "approve" && <ConfirmDialog title="Approve this gradesheet?" message={`Approving records your signature${p.acting_as ? ` as ${p.acting_as}` : ""} with the current date and time.`} confirmLabel="Approve" confirmClassName="bg-green-600 hover:bg-green-700 text-white" onConfirm={() => approve.mutate()} onCancel={() => setConfirm(null)} />}
      {revertOpen && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-lg font-bold text-gray-900 mb-2">Revert gradesheet</h3>
            <p className="text-sm text-gray-700 mb-3">The gradesheet returns to its preparing instructor for correction. A reason is required.</p>
            <textarea value={remark} onChange={(e) => setRemark(e.target.value)} rows={4} placeholder="Reason for reverting" className="w-full border border-gray-200 rounded-lg px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-red-400" />
            <div className="flex gap-3 mt-4">
              <button onClick={() => { setRevertOpen(false); setRemark(""); }} className="flex-1 py-2.5 border border-gray-200 rounded-xl font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
              <button onClick={() => revert.mutate()} disabled={!remark.trim() || revert.isPending} className="flex-1 py-2.5 bg-red-600 text-white rounded-xl font-bold hover:bg-red-700 disabled:opacity-50">Revert</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
