"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { FlaskConical, Plus, Loader2, Eye, X, CheckCircle2, XCircle, UserPlus, Trash2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { committeeRoleLabel, COMMITTEE_MEMBER_ROLES } from "@/lib/utils";

interface CommitteeMemberOut {
  id: string; faculty_id: string; faculty_name: string | null;
  designation: string | null; department_name: string | null;
  role: string; accepted: boolean | null; remark: string | null;
  major_advisor_count?: number; member_count?: number;
}
interface CommitteeListItem {
  id: string; student_id: string; student_name: string | null; student_roll: string | null;
  student_session: string | null; program_name: string | null; program_level: string | null;
  department_name: string | null; college_name: string | null;
  research_title: string | null; research_area: string | null;
  stage: string; status_label: string; is_locked: boolean;
  revert_remark: string | null; reverted_at: string | null;
  members: CommitteeMemberOut[];
  my_role?: string | null;
}
interface CommitteeDetail extends CommitteeListItem { can_manage_members: boolean; can_propose_major_advisor: boolean; }
interface UserOpt { id: string; full_name: string; role: string; designation: string | null; department_id: string | null; }
interface CapacityInfo { capacity: number; current: number; available: number; }
interface CreditDetails {
  student: { name: string | null; roll_no: string | null; department_name: string | null; program_name: string | null };
  courses: { offering_id: string; course_number: string; course_title: string; course_credit: string; semester_name: string | null; academic_year: string | null; enrollment_status: string }[];
  credit_summary: { total_credit_taken: number };
}

const STAGE_STYLE: Record<string, string> = {
  major_advisor_pending: "bg-amber-100 text-amber-700",
  member_selection: "bg-blue-100 text-blue-700",
  members_pending: "bg-blue-100 text-blue-700",
  hod_pending: "bg-purple-100 text-purple-700",
  hod_approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
};
// Roles allowed to call GET /auth/users (must match auth.py's list_users RBAC).
const USER_LOOKUP_ROLES = ["super_admin", "academic_admin", "registrar", "hod", "examiner"];

function StatusBadge({ stage, label }: { stage: string; label: string }) {
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold ${STAGE_STYLE[stage] ?? "bg-gray-100 text-gray-600"}`}>{label}</span>;
}

function RevertNotice({ remark }: { remark: string | null }) {
  if (!remark) return null;
  return (
    <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-800">
      <span className="font-bold">Reverted — reason: </span>{remark}
    </div>
  );
}

export default function ResearchPage() {
  const role = useRole();
  if (role === "student") return <StudentCommitteeView />;
  return <StaffCommitteeView />;
}

// ── Student: read-only own committee ────────────────────────────────────────────

function StudentCommitteeView() {
  const user = useUser();
  const { data: committee, isLoading, isError } = useQuery<CommitteeDetail>({
    queryKey: ["ams-my-committee", user?.id],
    queryFn: async () => (await api.get(`/research/committees/student/${user?.id}`)).data,
    enabled: !!user,
    retry: false,
  });
  const { data: credit } = useQuery<CreditDetails>({
    queryKey: ["ams-credit-details", user?.id],
    queryFn: async () => (await api.get(`/credit-details/student/${user?.id}`)).data,
    enabled: !!user,
    retry: false,
  });

  if (isLoading) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><FlaskConical size={24} className="text-[#0D6E6E]" />Advisory Committee</h1>
      <p className="text-gray-700 text-sm mb-6">Your PG/PhD advisory committee and research courses</p>

      {isError || !committee ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-10 text-center text-gray-600">
          <FlaskConical size={40} className="mx-auto mb-3 opacity-30" />
          <p>No Advisory Committee has been formed for you yet.</p>
        </div>
      ) : (
        <div className="space-y-6">
          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-bold text-gray-800">Student Information</h2>
              <StatusBadge stage={committee.stage} label={committee.status_label} />
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div><p className="text-gray-500">Student Name</p><p className="font-semibold">{committee.student_name ?? "—"}</p></div>
              <div><p className="text-gray-500">Roll No.</p><p className="font-semibold font-mono">{committee.student_roll ?? "—"}</p></div>
              <div><p className="text-gray-500">Department</p><p className="font-semibold">{committee.department_name ?? "—"}</p></div>
              <div><p className="text-gray-500">Student Session</p><p className="font-semibold">{committee.student_session ?? "—"}</p></div>
            </div>
            {committee.stage === "reverted" && <div className="mt-3"><RevertNotice remark={committee.revert_remark} /></div>}
          </div>

          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <h2 className="font-bold text-gray-800 mb-3">Student Research Course</h2>
            {!credit || credit.courses.length === 0 ? (
              <p className="text-sm text-gray-600 text-center py-6">No courses recorded yet.</p>
            ) : (
              <div className="w-full overflow-x-auto">
              <table className="w-full text-sm min-w-[700px]">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["Course Number", "Course Title", "Course Credit", "Credit Type", "Session", "Status"].map((h) => (
                    <th key={h} className="text-left px-3 py-2 font-semibold text-gray-700">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {credit.courses.map((c, i) => (
                    <tr key={c.offering_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-3 py-2 font-mono text-xs font-bold text-[#0D6E6E]">{c.course_number}</td>
                      <td className="px-3 py-2">{c.course_title}</td>
                      <td className="px-3 py-2 font-mono">{c.course_credit}</td>
                      <td className="px-3 py-2 text-gray-400">—</td>
                      <td className="px-3 py-2">{c.semester_name ?? "—"} {c.academic_year ?? ""}</td>
                      <td className="px-3 py-2 capitalize">{c.enrollment_status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4 pt-4 border-t border-gray-100 text-sm">
              <div><p className="text-gray-500">Total Credit Taken</p><p className="text-lg font-bold text-[#0D6E6E]">{credit?.credit_summary.total_credit_taken ?? "—"}</p></div>
              <div><p className="text-gray-500">Thesis Evaluation</p><p className="text-lg font-bold text-gray-400">—</p></div>
              <div><p className="text-gray-500">Total</p><p className="text-lg font-bold text-gray-400">—</p></div>
              <div><p className="text-gray-500">Remaining Credit</p><p className="text-lg font-bold text-gray-400">—</p></div>
            </div>
          </div>

          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <h2 className="font-bold text-gray-800 mb-3">Advisory Members</h2>
            <MembersList members={committee.members} />
          </div>
        </div>
      )}
    </div>
  );
}

function MembersList({ members }: { members: CommitteeMemberOut[] }) {
  if (members.length === 0) return <p className="text-sm text-gray-600 text-center py-6">No members yet.</p>;
  return (
    <div className="space-y-2">
      {members.map((m) => (
        <div key={m.id} className="flex items-center justify-between p-3 bg-gray-50 rounded-xl text-sm">
          <div>
            <p className="font-semibold text-gray-900">{m.faculty_name ?? "—"}</p>
            <p className="text-gray-600">{committeeRoleLabel(m.role)}</p>
            <p className="text-xs text-gray-500 mt-0.5">Major Advisor of {m.major_advisor_count ?? 0} · Member of {m.member_count ?? 0}</p>
          </div>
          <div className="text-right">
            {m.accepted === true && <span className="text-green-600 text-xs font-semibold">Accepted</span>}
            {m.accepted === false && <span className="text-red-500 text-xs font-semibold">Declined</span>}
            {m.accepted === null && <span className="text-amber-600 text-xs font-semibold">Pending</span>}
            {m.remark && <p className="text-xs text-gray-500 mt-0.5 max-w-[180px]">{m.remark}</p>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Staff (HOD / Faculty / Admin) ───────────────────────────────────────────────

function StaffCommitteeView() {
  const role = useRole();
  const currentUser = useUser();
  const qc = useQueryClient();
  const canPropose = ["super_admin", "academic_admin", "hod"].includes(role ?? "");
  const canLookupUsers = USER_LOOKUP_ROLES.includes(role ?? "");

  const [showPropose, setShowPropose] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showAddMember, setShowAddMember] = useState(false);
  const [proposeForm, setProposeForm] = useState({ student_id: "", major_advisor_id: "", research_title: "", research_area: "" });
  const [memberForm, setMemberForm] = useState({ faculty_id: "", role: "member_major" });
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);
  const [remarkPrompt, setRemarkPrompt] = useState<{ title: string; onSubmit: (remark: string) => void } | null>(null);
  const [remarkText, setRemarkText] = useState("");

  const { data: committees = [], isLoading } = useQuery<CommitteeListItem[]>({
    queryKey: ["ams-committees"],
    queryFn: async () => (await api.get("/research/committees")).data,
  });

  const { data: selected } = useQuery<CommitteeDetail>({
    queryKey: ["ams-committee-detail", selectedId],
    queryFn: async () => (await api.get(`/research/committees/student/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  const { data: allUsers = [] } = useQuery<UserOpt[]>({
    queryKey: ["ams-users"],
    queryFn: async () => (await api.get("/auth/users")).data,
    enabled: canPropose || canLookupUsers,
  });

  // Fix: a Major Advisor with role=FACULTY/RESEARCH_SUPERVISOR has no access
  // to the general /auth/users directory (admin/HOD-only) and previously saw
  // "Faculty lookup requires admin or HOD access" when trying to add committee
  // members despite being allowed to manage them. This committee-scoped
  // endpoint is authorized the same way add_member itself is (accepted Major
  // Advisor of THIS committee, or admin) — see research.py.
  const { data: eligibleFaculty = [] } = useQuery<UserOpt[]>({
    queryKey: ["ams-committee-eligible-faculty", selected?.id],
    queryFn: async () => (await api.get(`/research/committees/${selected?.id}/eligible-faculty`)).data,
    enabled: !!selected?.id && selected.can_manage_members && !canLookupUsers,
  });

  const { data: capacity } = useQuery<CapacityInfo>({
    queryKey: ["ams-advisor-capacity", proposeForm.major_advisor_id],
    queryFn: async () => (await api.get(`/research/committees/faculty/${proposeForm.major_advisor_id}/capacity`)).data,
    enabled: !!proposeForm.major_advisor_id,
  });

  const students = allUsers.filter((u) => u.role === "student");
  const facultyOptions = allUsers.filter((u) => ["faculty", "hod", "research_supervisor"].includes(u.role));
  // Add Member modal only: admin/HOD keep using the full directory above;
  // a non-admin accepted Major Advisor uses the committee-scoped list instead.
  const addMemberFacultyOptions = canLookupUsers ? facultyOptions : eligibleFaculty;
  const canPickAddMemberFaculty = canLookupUsers || (selected?.can_manage_members ?? false);

  function invalidateAll() {
    qc.invalidateQueries({ queryKey: ["ams-committees"] });
    qc.invalidateQueries({ queryKey: ["ams-committee-detail", selectedId] });
  }

  const proposeMajorAdvisor = useMutation({
    mutationFn: () => api.post("/research/committees", proposeForm),
    onSuccess: () => {
      toast.success("Major Advisor proposed. Awaiting their response.");
      invalidateAll(); setShowPropose(false);
      setProposeForm({ student_id: "", major_advisor_id: "", research_title: "", research_area: "" });
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to propose Major Advisor."),
  });

  const respondAsMajorAdvisor = useMutation({
    mutationFn: ({ id, accepted, remark }: { id: string; accepted: boolean; remark?: string }) =>
      api.patch(`/research/committees/${id}/major-advisor-response`, { accepted, remark }),
    onSuccess: (_, { accepted }) => {
      toast.success(accepted ? "Major Advisor role accepted." : "Proposal declined.");
      invalidateAll();
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to respond."),
  });

  const reassignMajorAdvisor = useMutation({
    mutationFn: ({ id, major_advisor_id }: { id: string; major_advisor_id: string }) =>
      api.post(`/research/committees/${id}/reassign-major-advisor`, { major_advisor_id }),
    onSuccess: () => { toast.success("New Major Advisor proposed."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to reassign."),
  });

  const addMember = useMutation({
    mutationFn: () => api.post(`/research/committees/${selected?.id}/members`, memberForm),
    onSuccess: () => {
      toast.success("Member added.");
      invalidateAll(); setShowAddMember(false);
      setMemberForm({ faculty_id: "", role: "member_major" });
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to add member."),
  });

  const removeMember = useMutation({
    mutationFn: (memberId: string) => api.delete(`/research/committees/${selected?.id}/members/${memberId}`),
    onSuccess: () => { toast.success("Member removed."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to remove member."),
  });

  const respondAsMember = useMutation({
    mutationFn: ({ memberId, accepted, remark }: { memberId: string; accepted: boolean; remark?: string }) =>
      api.patch(`/research/committees/${selected?.id}/members/${memberId}/accept`, null, { params: { accepted, remark } }),
    onSuccess: () => { toast.success("Response recorded."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to respond."),
  });

  const hodApproval = useMutation({
    mutationFn: ({ id, approved, remark }: { id: string; approved: boolean; remark?: string }) =>
      api.patch(`/research/committees/${id}/hod-approval`, { approved, remark }),
    onSuccess: (_, { approved }) => { toast.success(approved ? "Committee approved." : "Committee reverted."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to record HOD decision."),
  });

  const myMajorAdvisorRow = selected?.members.find((m) => m.role === "major_advisor" && m.faculty_id === currentUser?.id);

  if (isLoading) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><FlaskConical size={24} className="text-[#0D6E6E]" />Advisory Committee</h1>
          <p className="text-gray-700 text-base mt-1">PG &amp; PhD research advisory committees</p>
        </div>
        {canPropose && (
          <button onClick={() => setShowPropose(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Propose Major Advisor
          </button>
        )}
      </div>

      {/* Committee table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
        {committees.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><FlaskConical size={40} className="mx-auto mb-3 opacity-30" /><p>No advisory committees found.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["Sl No", "Advisory Role", "Student Name", "Degree", "College", "Status", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {committees.map((c, i) => (
                <tr key={c.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-3 text-gray-700">{c.my_role ? committeeRoleLabel(c.my_role) : "—"}</td>
                  <td className="px-4 py-3 font-medium text-gray-900">{c.student_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{c.program_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{c.college_name ?? "—"}</td>
                  <td className="px-4 py-3"><StatusBadge stage={c.stage} label={c.status_label} /></td>
                  <td className="px-4 py-3">
                    <button onClick={() => setSelectedId(c.student_id)}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                      <Eye size={13} /> View Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Propose Major Advisor modal */}
      {showPropose && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-1">Propose Major Advisor</h3>
            <p className="text-sm text-gray-600 mb-4">HOD selects the Major Advisor; the Major Advisor then selects the remaining committee members.</p>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Student</label>
                <select value={proposeForm.student_id} onChange={(e) => setProposeForm((f) => ({ ...f, student_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select student…</option>
                  {students.map((s) => <option key={s.id} value={s.id}>{s.full_name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Major Advisor</label>
                <select value={proposeForm.major_advisor_id} onChange={(e) => setProposeForm((f) => ({ ...f, major_advisor_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select faculty…</option>
                  {facultyOptions.map((f) => <option key={f.id} value={f.id}>{f.full_name}{f.designation ? ` — ${f.designation}` : ""}</option>)}
                </select>
                {capacity && (
                  <p className={`text-xs mt-1 ${capacity.available > 0 ? "text-gray-600" : "text-red-600 font-semibold"}`}>
                    Currently advising {capacity.current}/{capacity.capacity} students {capacity.available > 0 ? `(${capacity.available} slot(s) available)` : "(at capacity)"}
                  </p>
                )}
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Research Title</label>
                <input value={proposeForm.research_title} onChange={(e) => setProposeForm((f) => ({ ...f, research_title: e.target.value }))} placeholder="Thesis / Research title"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Research Area</label>
                <input value={proposeForm.research_area} onChange={(e) => setProposeForm((f) => ({ ...f, research_area: e.target.value }))} placeholder="Plant Breeding, Animal Nutrition…"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setShowPropose(false)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              <button onClick={() => proposeMajorAdvisor.mutate()}
                disabled={proposeMajorAdvisor.isPending || !proposeForm.student_id || !proposeForm.major_advisor_id || (!!capacity && capacity.available <= 0)}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                {proposeMajorAdvisor.isPending ? "Proposing…" : "Propose"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* View Details modal */}
      {selectedId && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] overflow-y-auto">
            {!selected ? (
              <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
            ) : (
              <>
                <div className="flex items-start justify-between px-6 py-5 border-b border-gray-100">
                  <div>
                    <h3 className="text-xl font-bold text-gray-900">{selected.student_name ?? "—"}</h3>
                    {selected.research_title && <p className="text-sm text-gray-600 mt-0.5">{selected.research_title}</p>}
                  </div>
                  <button onClick={() => setSelectedId(null)} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
                </div>

                <div className="p-6 space-y-6">
                  {/* Student information */}
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4 bg-gray-50 rounded-xl p-4">
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Roll No</p><p className="text-sm text-gray-800 font-mono">{selected.student_roll ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Department</p><p className="text-sm text-gray-800">{selected.department_name ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Student Session</p><p className="text-sm text-gray-800">{selected.student_session ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Status</p><StatusBadge stage={selected.stage} label={selected.status_label} /></div>
                  </div>

                  {selected.stage === "reverted" && <RevertNotice remark={selected.revert_remark} />}

                  {/* Major Advisor response (Stage 1) */}
                  {myMajorAdvisorRow && myMajorAdvisorRow.accepted === null && selected.stage === "major_advisor_pending" && (
                    <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
                      <p className="text-sm font-semibold text-amber-900 mb-3">You have been proposed as Major Advisor for this student.</p>
                      <div className="flex gap-3">
                        <button onClick={() => respondAsMajorAdvisor.mutate({ id: selected.id, accepted: true })}
                          className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white text-sm font-semibold rounded-lg hover:bg-green-700">
                          <CheckCircle2 size={14} /> Accept
                        </button>
                        <button onClick={() => setRemarkPrompt({ title: "Decline Major Advisor Role", onSubmit: (remark) => respondAsMajorAdvisor.mutate({ id: selected.id, accepted: false, remark }) })}
                          className="flex items-center gap-1.5 px-4 py-2 border border-red-300 text-red-700 text-sm font-semibold rounded-lg hover:bg-red-50">
                          <XCircle size={14} /> Decline
                        </button>
                      </div>
                    </div>
                  )}

                  {/* HOD reassignment after decline */}
                  {selected.can_propose_major_advisor && selected.stage === "reverted" && myMajorAdvisorRow === undefined && (
                    <ReassignPanel
                      facultyOptions={facultyOptions}
                      onSubmit={(major_advisor_id) => reassignMajorAdvisor.mutate({ id: selected.id, major_advisor_id })}
                      pending={reassignMajorAdvisor.isPending}
                    />
                  )}

                  {/* Committee Members */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="text-base font-bold text-gray-900">Advisory Members</h4>
                      {selected.can_manage_members && (selected.stage === "member_selection" || selected.stage === "members_pending") && (
                        <button onClick={() => setShowAddMember(true)}
                          className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                          <UserPlus size={14} /> Add Member
                        </button>
                      )}
                    </div>
                    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden overflow-x-auto">
                      {selected.members.length === 0 ? (
                        <p className="text-sm text-gray-600 text-center py-6">No members yet.</p>
                      ) : (
                        <table className="w-full text-sm">
                          <thead className="bg-gray-50 border-b border-gray-200">
                            <tr>{["Name", "Designation", "Department", "Advisory Role", "Status", ...(selected.can_manage_members ? ["Action"] : [])].map((h) => (
                              <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
                            ))}</tr>
                          </thead>
                          <tbody>
                            {selected.members.map((m, i) => {
                              const isMine = currentUser?.id === m.faculty_id && m.role !== "major_advisor";
                              return (
                                <tr key={m.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                                  <td className="px-4 py-2.5 font-medium text-gray-900">{m.faculty_name ?? "—"}</td>
                                  <td className="px-4 py-2.5 text-gray-600">{m.designation ?? "—"}</td>
                                  <td className="px-4 py-2.5 text-gray-600">{m.department_name ?? "—"}</td>
                                  <td className="px-4 py-2.5">
                                    <span className="text-gray-800">{committeeRoleLabel(m.role)}</span>
                                    <p className="text-xs text-gray-500">Major Advisor of {m.major_advisor_count ?? 0} · Member of {m.member_count ?? 0}</p>
                                  </td>
                                  <td className="px-4 py-2.5">
                                    {m.accepted === true && <span className="text-green-600 text-sm font-semibold">Accepted</span>}
                                    {m.accepted === false && <span className="text-red-500 text-sm font-semibold">Declined{m.remark ? ` — ${m.remark}` : ""}</span>}
                                    {m.accepted === null && isMine && (
                                      <div className="flex gap-1.5">
                                        <button onClick={() => respondAsMember.mutate({ memberId: m.id, accepted: true })}
                                          className="p-1 text-green-600 hover:bg-green-50 rounded"><CheckCircle2 size={16} /></button>
                                        <button onClick={() => setRemarkPrompt({ title: "Decline Membership", onSubmit: (remark) => respondAsMember.mutate({ memberId: m.id, accepted: false, remark }) })}
                                          className="p-1 text-red-500 hover:bg-red-50 rounded"><XCircle size={16} /></button>
                                      </div>
                                    )}
                                    {m.accepted === null && !isMine && <span className="text-amber-600 text-sm font-semibold">Pending</span>}
                                  </td>
                                  {selected.can_manage_members && (
                                    <td className="px-4 py-2.5">
                                      {m.role !== "major_advisor" && (
                                        <button onClick={() => removeMember.mutate(m.id)} className="p-1 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"><Trash2 size={14} /></button>
                                      )}
                                    </td>
                                  )}
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>

                  {/* HOD approval */}
                  {selected.can_propose_major_advisor && selected.stage === "hod_pending" && (
                    <div className="bg-purple-50 border border-purple-200 rounded-xl p-4">
                      <p className="text-sm font-semibold text-purple-900 mb-3">All committee members have accepted. Awaiting HOD approval.</p>
                      <div className="flex gap-3">
                        <button onClick={() => setConfirm({
                          action: () => hodApproval.mutate({ id: selected.id, approved: true }),
                          title: "Approve Committee", message: `Approve the Advisory Committee for ${selected.student_name}?`,
                          confirmLabel: "Yes, Approve", confirmClassName: "bg-green-600 hover:bg-green-700 text-white",
                        })} className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white text-sm font-semibold rounded-lg hover:bg-green-700">
                          <CheckCircle2 size={14} /> Approve
                        </button>
                        <button onClick={() => setRemarkPrompt({ title: "Revert to Member Stage", onSubmit: (remark) => hodApproval.mutate({ id: selected.id, approved: false, remark }) })}
                          className="flex items-center gap-1.5 px-4 py-2 border border-red-300 text-red-700 text-sm font-semibold rounded-lg hover:bg-red-50">
                          <XCircle size={14} /> Revert
                        </button>
                      </div>
                    </div>
                  )}

                  {selected.stage === "hod_approved" && (
                    <div className="bg-green-50 border border-green-200 rounded-xl p-4 text-sm font-semibold text-green-800">
                      Advisory Committee approved by HOD. (I/C Academic Cell and DPGS stages are not yet available in this demo — see documentation.)
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Add Member modal */}
      {showAddMember && selected && (
        <div className="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-4">Add Committee Member</h3>
            <p className="text-sm text-gray-600 mb-3">For: {selected.student_name}</p>
            {!canPickAddMemberFaculty ? (
              <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-3">
                Faculty lookup requires admin or HOD access. Ask an administrator to add this committee member for you.
              </p>
            ) : (
              <div className="space-y-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Faculty</label>
                  <select value={memberForm.faculty_id} onChange={(e) => setMemberForm((f) => ({ ...f, faculty_id: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">Select faculty…</option>
                    {addMemberFacultyOptions.map((f) => <option key={f.id} value={f.id}>{f.full_name}{f.designation ? ` — ${f.designation}` : ""}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Role</label>
                  <div className="grid grid-cols-2 gap-2">
                    {COMMITTEE_MEMBER_ROLES.map((r) => (
                      <button key={r} type="button" onClick={() => setMemberForm((f) => ({ ...f, role: r }))}
                        className={`py-2 text-xs rounded-xl font-semibold border-2 transition-all ${memberForm.role === r ? "border-[#0D6E6E] bg-[#0D6E6E] text-white" : "border-gray-200 text-gray-600"}`}>
                        {committeeRoleLabel(r)}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}
            <div className="flex gap-3 mt-5">
              <button onClick={() => { setShowAddMember(false); setMemberForm({ faculty_id: "", role: "member_major" }); }}
                className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              {canPickAddMemberFaculty && (
                <button onClick={() => addMember.mutate()} disabled={addMember.isPending || !memberForm.faculty_id}
                  className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                  {addMember.isPending ? "Adding…" : "Add Member"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Mandatory-remark prompt (revert/decline) */}
      {remarkPrompt && (
        <div className="fixed inset-0 bg-black/40 z-[70] flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-lg font-bold mb-2">{remarkPrompt.title}</h3>
            <p className="text-sm text-gray-600 mb-3">A reason is required and will be shown to the relevant user.</p>
            <textarea value={remarkText} onChange={(e) => setRemarkText(e.target.value)} rows={3}
              placeholder="Reason…" className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] resize-none" />
            <div className="flex gap-3 mt-4">
              <button onClick={() => { setRemarkPrompt(null); setRemarkText(""); }} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => { if (!remarkText.trim()) { toast.error("A remark is required."); return; } remarkPrompt.onSubmit(remarkText.trim()); setRemarkPrompt(null); setRemarkText(""); }}
                className="flex-1 py-2.5 bg-red-600 text-white rounded-xl text-sm font-bold hover:bg-red-700">Submit</button>
            </div>
          </div>
        </div>
      )}

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}

function ReassignPanel({ facultyOptions, onSubmit, pending }: { facultyOptions: UserOpt[]; onSubmit: (facultyId: string) => void; pending: boolean }) {
  const [id, setId] = useState("");
  return (
    <div className="bg-blue-50 border border-blue-200 rounded-xl p-4">
      <p className="text-sm font-semibold text-blue-900 mb-3">The proposed Major Advisor declined. Propose a different Major Advisor.</p>
      <div className="flex gap-2">
        <select value={id} onChange={(e) => setId(e.target.value)}
          className="flex-1 border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
          <option value="">Select faculty…</option>
          {facultyOptions.map((f) => <option key={f.id} value={f.id}>{f.full_name}{f.designation ? ` — ${f.designation}` : ""}</option>)}
        </select>
        <button onClick={() => id && onSubmit(id)} disabled={!id || pending}
          className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-bold disabled:opacity-60">
          {pending ? "Proposing…" : "Propose"}
        </button>
      </div>
    </div>
  );
}
