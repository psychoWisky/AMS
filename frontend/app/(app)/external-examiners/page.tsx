"use client";
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useUser } from "@/stores/auth.store";
import { UserSearch, Loader2, Plus } from "lucide-react";
import {
  ProposalTable, RevertNotice, SelectionStatusBadge, StageTimeline, StudentInfoCard,
  apiErrorMessage, type SelectionDetail,
} from "@/components/ui/external-examiner-parts";

// Major Advisor's own page. There is NO student route/page for this module at all — the student has
// zero visibility (confirmed requirement); the Major Advisor prepares the list on their behalf. The
// backend independently re-verifies the caller is the student's accepted Major Advisor on every call —
// this page never sends a major_advisor_id, only a student_id chosen from the Major Advisor's OWN
// committees.

interface CommitteeRow {
  student_id: string; student_name: string | null; student_roll: string | null; program_level: string | null;
  my_role?: string; members: { faculty_id: string; role: string; accepted: boolean | null }[];
}
interface MySelectionRow {
  selection_id: string; student_id: string; student_name: string; student_roll: string | null;
  degree_level: string; status: string; status_label: string;
}

const REQUIRED = { PG: 3, PhD: 5 } as const;
const EMPTY_PROPOSAL = { name: "", specialization: "", designation: "", email: "", phone: "", institution: "" };

export default function ExternalExaminersPage() {
  const qc = useQueryClient();
  const me = useUser();
  const [studentId, setStudentId] = useState("");
  const [openSelectionId, setOpenSelectionId] = useState<string | null>(null);
  const [proposals, setProposals] = useState(Array.from({ length: 3 }, () => ({ ...EMPTY_PROPOSAL })));

  const { data: committees = [] } = useQuery<CommitteeRow[]>({
    queryKey: ["ams-my-committees"],
    queryFn: async () => (await api.get("/research/committees")).data,
  });
  const { data: mine = [], isLoading } = useQuery<MySelectionRow[]>({
    queryKey: ["ams-ee-mine"],
    queryFn: async () => (await api.get("/external-examiners/mine")).data,
  });
  const { data: detail } = useQuery<SelectionDetail>({
    queryKey: ["ams-ee-detail", openSelectionId],
    queryFn: async () => (await api.get(`/external-examiners/${openSelectionId}`)).data,
    enabled: !!openSelectionId,
  });

  const myAcceptedStudents = useMemo(
    () => committees.filter((c) => c.my_role === "major_advisor" && c.members.some((m) => m.faculty_id === me?.id && m.role === "major_advisor" && m.accepted === true)),
    [committees, me?.id],
  );
  const existingStudentIds = new Set(mine.map((r) => r.student_id));
  const eligibleForNew = myAcceptedStudents.filter((c) => !existingStudentIds.has(c.student_id) || mine.find((r) => r.student_id === c.student_id)?.status === "reverted" || mine.find((r) => r.student_id === c.student_id)?.status === "draft");
  const selectedStudent = myAcceptedStudents.find((c) => c.student_id === studentId);
  const requiredCount = selectedStudent?.program_level === "PhD" ? REQUIRED.PhD : selectedStudent?.program_level === "PG" ? REQUIRED.PG : null;

  function resetForm(count: number) {
    setProposals(Array.from({ length: count }, () => ({ ...EMPTY_PROPOSAL })));
  }
  function pickStudent(id: string) {
    setStudentId(id);
    const c = myAcceptedStudents.find((x) => x.student_id === id);
    resetForm(c?.program_level === "PhD" ? REQUIRED.PhD : REQUIRED.PG);
  }
  function updateProposal(i: number, field: keyof typeof EMPTY_PROPOSAL, value: string) {
    setProposals((p) => p.map((row, idx) => (idx === i ? { ...row, [field]: value } : row)));
  }

  const submit = useMutation({
    mutationFn: () => api.post("/external-examiners", { student_id: studentId, proposals }),
    onSuccess: () => {
      toast.success("External Examiner list submitted for approval.");
      setStudentId(""); resetForm(3);
      qc.invalidateQueries({ queryKey: ["ams-ee-mine"] });
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not submit the External Examiner list.")),
  });

  const canSubmit = requiredCount !== null && proposals.length === requiredCount &&
    proposals.every((p) => Object.values(p).every((v) => v.trim()));

  return (
    <div className="p-6 max-w-5xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><UserSearch size={24} className="text-[#0D6E6E]" />External Examiners</h1>
        <p className="text-gray-700 text-base mt-1">Propose External Examiners for a student&apos;s thesis evaluation, as their accepted Major Advisor.</p>
      </div>

      <section className="space-y-2">
        <h2 className="text-lg font-bold text-gray-900">Your students</h2>
        {isLoading ? <Loader2 className="animate-spin text-gray-500" /> : mine.length === 0 ? (
          <p className="text-sm text-gray-600">No External Examiner selection has been started for any of your students yet.</p>
        ) : (
          <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Student", "Roll No.", "Degree", "Status", ""].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
              <tbody>
                {mine.map((r) => (
                  <tr key={r.selection_id} className="border-b border-gray-50 last:border-0">
                    <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                    <td className="px-4 py-2.5 text-gray-600">{r.student_roll ?? "—"}</td>
                    <td className="px-4 py-2.5">{r.degree_level}</td>
                    <td className="px-4 py-2.5"><SelectionStatusBadge status={r.status} label={r.status_label} /></td>
                    <td className="px-4 py-2.5"><button onClick={() => setOpenSelectionId(r.selection_id)} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">View</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bg-white rounded-2xl border border-gray-200 p-5 space-y-4">
        <h2 className="text-lg font-bold text-gray-900">Propose examiners for a new student</h2>
        <div>
          <label className="block text-sm font-semibold text-gray-700 mb-1">Student</label>
          <select value={studentId} onChange={(e) => pickStudent(e.target.value)} className="w-full md:w-96 border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
            <option value="">Select a student…</option>
            {eligibleForNew.map((c) => <option key={c.student_id} value={c.student_id}>{c.student_name} ({c.student_roll}) — {c.program_level}</option>)}
          </select>
          {eligibleForNew.length === 0 && <p className="text-xs text-gray-500 mt-1">You have no eligible students (accepted Major Advisor, PG/PhD, no selection currently under approval or approved).</p>}
        </div>

        {studentId && requiredCount && (
          <>
            <p className="text-sm text-gray-600">{selectedStudent?.program_level} requires exactly <b>{requiredCount}</b> proposed examiners. The Vice Chancellor will later select {requiredCount === 3 ? "1" : "2"} of them.</p>
            <div className="space-y-4">
              {proposals.map((p, i) => (
                <div key={i} className="border border-gray-200 rounded-xl p-4">
                  <p className="text-sm font-bold text-gray-800 mb-2">Examiner {i + 1}</p>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    {(["name", "specialization", "designation", "email", "phone", "institution"] as const).map((field) => (
                      <div key={field}>
                        <label className="block text-xs font-semibold text-gray-600 mb-1 capitalize">{field}</label>
                        <input value={p[field]} onChange={(e) => updateProposal(i, field, e.target.value)}
                          className="w-full border border-gray-300 rounded-lg px-2.5 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <button onClick={() => submit.mutate()} disabled={!canSubmit || submit.isPending}
              className="flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#178F8F] disabled:opacity-50">
              {submit.isPending ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Submit for Approval
            </button>
          </>
        )}
      </section>

      {openSelectionId && detail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setOpenSelectionId(null)}>
          <div className="bg-white rounded-2xl max-w-4xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-gray-900">{detail.student.name}&apos;s External Examiner Selection</h2>
              <SelectionStatusBadge status={detail.status} label={detail.status_label} />
            </div>
            <StudentInfoCard student={detail.student} degreeLevel={detail.degree_level} />
            {detail.revert_info && <RevertNotice info={detail.revert_info} />}
            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">Proposed examiners</p>
              <ProposalTable proposals={detail.proposals} />
            </div>
            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">Approval progress</p>
              <StageTimeline stages={detail.stages} />
              <p className="text-sm mt-2 text-gray-700">{detail.selection_completed ? "External examiner selection completed." : "Vice Chancellor selection not yet completed."}</p>
            </div>
            <div className="flex justify-end">
              <button onClick={() => setOpenSelectionId(null)} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
