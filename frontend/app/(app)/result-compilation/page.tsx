"use client";
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, ClipboardCheck, Eye, Loader2, Send } from "lucide-react";
import { api } from "@/services/api";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { apiErrorMessage } from "@/components/ui/gradesheet-parts";
import { ResultDetailModal } from "@/components/ui/result-parts";

// Controller of Examination — Result Compilation. Course-wise gradesheets are finalized
// separately (Gradesheet Approvals); here the CoE compiles each student's semester result from
// them and MANUALLY chooses Pass or Pass with Backlogs (AMS never infers it), then publishes.
// Publishing only makes the result visible to the student — AMS applies no promotion,
// backlog-limit or semester-change rule (none is confirmed by AVFU yet).

interface SemesterOpt { semester_id: string; name: string; academic_year: string | null }
interface StudentRow {
  student_id: string; name: string | null; roll_no: string | null;
  courses_total: number; courses_finalized: number; pending_courses: string[]; ready: boolean;
  result: { id: string; status: "compiled" | "published"; result_status: string; gpa: string | null } | null;
}
type Choice = "" | "pass" | "pass_with_backlogs";

const th = "text-left px-4 py-3 font-semibold text-gray-700";
const NO_STUDENTS: StudentRow[] = [];

export default function ResultCompilationPage() {
  const qc = useQueryClient();
  const [semesterId, setSemesterId] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [choice, setChoice] = useState<Record<string, Choice>>({});
  const [viewId, setViewId] = useState<string | null>(null);
  const [confirmPublish, setConfirmPublish] = useState(false);
  const [problems, setProblems] = useState<{ student_id: string; detail: string }[]>([]);

  const { data: semesters = [], isLoading: semLoading } = useQuery<SemesterOpt[]>({
    queryKey: ["ams-coe-semesters"],
    queryFn: async () => (await api.get("/results/coe/semesters")).data,
  });
  const { data: studentsData, isLoading, isError } = useQuery<StudentRow[]>({
    queryKey: ["ams-coe-students", semesterId],
    queryFn: async () => (await api.get(`/results/coe/semesters/${semesterId}/students`)).data,
    enabled: !!semesterId,
  });

  // Reset the per-row choices/selection whenever a new student list arrives (derived while rendering).
  const students = studentsData ?? NO_STUDENTS;
  const [syncedFor, setSyncedFor] = useState<StudentRow[] | undefined>(undefined);
  if (studentsData && studentsData !== syncedFor) {
    setSyncedFor(studentsData);
    setChoice(Object.fromEntries(studentsData.map((s) => [s.student_id, (s.result?.result_status as Choice) ?? ""])));
    setSelected(new Set());
    setProblems([]);
  }

  const nameOf = (id: string) => students.find((s) => s.student_id === id)?.name ?? id;
  const refresh = () => qc.invalidateQueries({ queryKey: ["ams-coe-students", semesterId] });

  const compile = useMutation({
    mutationFn: async () => (await api.post("/results/coe/compile", {
      semester_id: semesterId,
      items: [...selected].map((id) => ({ student_id: id, result_status: choice[id] })),
    })).data as { compiled: unknown[]; errors: { student_id: string; detail: string }[] },
    onSuccess: (res) => {
      setProblems(res.errors);
      if (res.compiled.length) toast.success(`${res.compiled.length} result(s) compiled.`);
      if (res.errors.length) toast.error(`${res.errors.length} student(s) could not be compiled — see the list below.`);
      refresh();
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not compile the results.")),
  });
  const publishIds = students.filter((s) => selected.has(s.student_id) && s.result?.status === "compiled").map((s) => s.result!.id);
  const publish = useMutation({
    mutationFn: async () => (await api.post("/results/coe/publish", { result_ids: publishIds })).data as { published: string[]; errors: { detail: string }[] },
    onSuccess: (res) => { setConfirmPublish(false); toast.success(`${res.published.length} result(s) published.`); refresh(); },
    onError: (e) => { setConfirmPublish(false); toast.error(apiErrorMessage(e, "Could not publish the results.")); },
  });

  const toggle = (id: string) => setSelected((prev) => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const compilable = [...selected].filter((id) => { const s = students.find((x) => x.student_id === id); return s && s.ready && s.result?.status !== "published"; });
  const canCompile = selected.size > 0 && compilable.length === selected.size && [...selected].every((id) => choice[id]);

  return (
    <div className="p-6 w-full">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Result Compilation</h1>
          <p className="text-gray-700 text-base mt-1">Compile student-wise semester results from finalized gradesheets, choose Pass or Pass with Backlogs, then publish</p>
        </div>
        <Link href="/grading" className="px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl font-semibold hover:bg-[#E6F4F4]">Gradesheet Approvals</Link>
      </div>

      <div className="mb-4">
        <select value={semesterId} onChange={(e) => setSemesterId(e.target.value)} className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none min-w-[18rem]">
          <option value="">{semLoading ? "Loading semesters…" : "Select a semester"}</option>
          {semesters.map((s) => <option key={s.semester_id} value={s.semester_id}>{s.name} — {s.academic_year ?? "—"}</option>)}
        </select>
      </div>

      {semesterId && (
        <>
          <div className="flex flex-wrap gap-3 mb-4">
            <button onClick={() => compile.mutate()} disabled={!canCompile || compile.isPending} className="inline-flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#0a5858] disabled:opacity-50">
              {compile.isPending ? <Loader2 size={16} className="animate-spin" /> : <CheckCircle2 size={16} />} Compile selected
            </button>
            <button onClick={() => setConfirmPublish(true)} disabled={publishIds.length === 0} className="inline-flex items-center gap-2 px-4 py-2.5 bg-indigo-600 text-white rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-50"><Send size={16} /> Publish selected ({publishIds.length})</button>
            <p className="text-sm text-gray-600 self-center">Select students whose courses are all finalized, choose a result for each, then compile.</p>
          </div>
          {problems.length > 0 && (
            <div className="mb-4 bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-sm text-red-800">
              <p className="font-semibold mb-1">Not compiled:</p>
              <ul className="list-disc pl-5">{problems.map((p) => <li key={p.student_id}>{nameOf(p.student_id)} — {p.detail}</li>)}</ul>
            </div>
          )}
          <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
            {isLoading ? <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
              : isError ? <div className="text-center py-14 text-red-600">Could not load this semester&apos;s students.</div>
              : students.length === 0 ? <div className="text-center py-14 text-gray-600">No students have approved course registrations in this semester.</div>
              : (
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
                    {["", "SL NO", "Roll No", "Name", "Finalized courses", "Result", "Status", "GPA", "Action"].map((h, i) => <th key={i} className={th}>{h}</th>)}
                  </tr></thead>
                  <tbody>
                    {students.map((s, i) => {
                      const locked = s.result?.status === "published";
                      return (
                        <tr key={s.student_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                          <td className="px-4 py-3"><input type="checkbox" checked={selected.has(s.student_id)} disabled={locked} onChange={() => toggle(s.student_id)} aria-label={`Select ${s.name ?? "student"}`} /></td>
                          <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                          <td className="px-4 py-3 font-mono">{s.roll_no ?? "—"}</td>
                          <td className="px-4 py-3">{s.name ?? "—"}</td>
                          <td className="px-4 py-3">
                            <span className={s.ready ? "text-green-700 font-semibold" : "text-amber-700 font-semibold"}>{s.courses_finalized}/{s.courses_total}</span>
                            {!s.ready && <span className="block text-xs text-gray-500">Pending: {s.pending_courses.join(", ")}</span>}
                          </td>
                          <td className="px-4 py-3">
                            <select value={choice[s.student_id] ?? ""} disabled={locked || !s.ready} onChange={(e) => setChoice((p) => ({ ...p, [s.student_id]: e.target.value as Choice }))} className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm focus:outline-none disabled:bg-gray-100">
                              <option value="">Select…</option><option value="pass">Pass</option><option value="pass_with_backlogs">Pass with Backlogs</option>
                            </select>
                          </td>
                          <td className="px-4 py-3">{s.result ? <span className={`px-2.5 py-1 rounded-full text-sm font-semibold ${locked ? "bg-green-100 text-green-700" : "bg-blue-100 text-blue-700"}`}>{locked ? "Published" : "Compiled"}</span> : <span className="text-gray-500">Not compiled</span>}</td>
                          <td className="px-4 py-3 font-mono">{s.result?.gpa ?? "—"}</td>
                          <td className="px-4 py-3">{s.result && <button onClick={() => setViewId(s.result!.id)} className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]"><Eye size={13} /> View</button>}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
          </div>
        </>
      )}
      {viewId && <ResultDetailModal resultId={viewId} onClose={() => setViewId(null)} />}
      {confirmPublish && <ConfirmDialog title="Publish results?" message={`${publishIds.length} compiled result(s) will become visible to the students. A published result cannot be recompiled.`} confirmLabel="Publish" confirmClassName="bg-indigo-600 hover:bg-indigo-700 text-white" onConfirm={() => publish.mutate()} onCancel={() => setConfirmPublish(false)} />}
    </div>
  );
}
