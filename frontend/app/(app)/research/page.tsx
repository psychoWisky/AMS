"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { FlaskConical, Plus, Lock, Loader2, Eye, X, CheckCircle2, XCircle } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { committeeRoleLabel } from "@/lib/utils";

interface CommitteeMemberOut {
  id: string; faculty_id: string; faculty_name: string | null;
  designation: string | null; department_name: string | null;
  role: string; accepted: boolean | null;
  major_advisor_count?: number; member_count?: number;
}
interface CommitteeListItem {
  id: string; student_id: string; student_name: string | null; student_roll: string | null;
  program_name: string | null; program_level: string | null; department_name: string | null;
  research_title: string | null; research_area: string | null;
  status: string; is_locked: boolean;
  members: CommitteeMemberOut[];
}
interface CommitteeDetail extends CommitteeListItem { can_manage: boolean; }
interface UserOpt { id: string; full_name: string; role: string; designation: string | null; }

const STATUS_COLOR: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700", active: "bg-green-100 text-green-700",
  locked: "bg-amber-100 text-amber-700", dissolved: "bg-red-100 text-red-700",
};
// Roles allowed to call GET /auth/users (must match auth.py's list_users RBAC).
const USER_LOOKUP_ROLES = ["super_admin", "academic_admin", "registrar", "hod", "examiner"];

export default function ResearchPage() {
  const role = useRole();
  const currentUser = useUser();
  const qc = useQueryClient();
  const isAdmin = ["super_admin", "academic_admin", "hod"].includes(role ?? "");
  const canLock = ["super_admin", "academic_admin"].includes(role ?? "");
  const canLookupUsers = USER_LOOKUP_ROLES.includes(role ?? "");

  const [showCreate, setShowCreate] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showAddMember, setShowAddMember] = useState(false);
  const [form, setForm] = useState({ student_id: "", research_title: "", research_area: "" });
  const [memberForm, setMemberForm] = useState({ faculty_id: "", role: "member" });
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

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
    enabled: isAdmin || canLookupUsers,
  });

  const students = allUsers.filter((u) => u.role === "student");
  const facultyOptions = allUsers.filter((u) => ["faculty", "hod", "research_supervisor"].includes(u.role));

  const createCommittee = useMutation({
    mutationFn: () => api.post("/research/committees", form),
    onSuccess: () => { toast.success("Committee created."); qc.invalidateQueries({ queryKey: ["ams-committees"] }); setShowCreate(false); setForm({ student_id: "", research_title: "", research_area: "" }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const addMember = useMutation({
    mutationFn: () => api.post(`/research/committees/${selected?.id}/members`, memberForm),
    onSuccess: () => {
      toast.success("Member added.");
      qc.invalidateQueries({ queryKey: ["ams-committees"] });
      qc.invalidateQueries({ queryKey: ["ams-committee-detail", selectedId] });
      setShowAddMember(false);
      setMemberForm({ faculty_id: "", role: "member" });
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to add member."),
  });

  const lockCommittee = useMutation({
    mutationFn: (id: string) => api.patch(`/research/committees/${id}/lock`),
    onSuccess: () => {
      toast.success("Committee locked.");
      qc.invalidateQueries({ queryKey: ["ams-committees"] });
      qc.invalidateQueries({ queryKey: ["ams-committee-detail", selectedId] });
    },
  });

  const acceptMembership = useMutation({
    mutationFn: ({ committeeId, memberId, accepted }: { committeeId: string; memberId: string; accepted: boolean }) =>
      api.patch(`/research/committees/${committeeId}/members/${memberId}/accept?accepted=${accepted}`),
    onSuccess: () => {
      toast.success("Response recorded.");
      qc.invalidateQueries({ queryKey: ["ams-committees"] });
      qc.invalidateQueries({ queryKey: ["ams-committee-detail", selectedId] });
    },
    onError: () => toast.error("Failed to record response."),
  });

  function advisoryRoleFor(c: CommitteeListItem): string {
    if (role === "student") return "—";
    const mine = currentUser ? c.members.find((m) => m.faculty_id === currentUser.id) : undefined;
    if (mine) return committeeRoleLabel(mine.role);
    if (c.members.length === 0) return "—";
    const distinct = Array.from(new Set(c.members.map((m) => committeeRoleLabel(m.role))));
    return distinct.join(", ");
  }

  if (isLoading) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><FlaskConical size={24} className="text-[#0D6E6E]" />Advisory Committees</h1>
          <p className="text-gray-700 text-base mt-1">PG &amp; PhD research advisory committees</p>
        </div>
        {isAdmin && (
          <button onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> New Committee
          </button>
        )}
      </div>

      {/* Committee table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
        {committees.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><FlaskConical size={40} className="mx-auto mb-3 opacity-30" /><p>No advisory committees found.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["Sl No", "Advisory Role", "Student Name", "Roll No", "Degree", "Department", "Status", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {committees.map((c, i) => (
                <tr key={c.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-3 text-gray-700">{advisoryRoleFor(c)}</td>
                  <td className="px-4 py-3 font-medium text-gray-900">{c.student_name ?? "—"}</td>
                  <td className="px-4 py-3 font-mono text-sm">{c.student_roll ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{c.program_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{c.department_name ?? "—"}</td>
                  <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[c.status] ?? "bg-gray-100 text-gray-600"}`}>{c.status}</span></td>
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

      {/* Create Committee modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-4">Form Advisory Committee</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Student</label>
                <select value={form.student_id} onChange={(e) => setForm((f) => ({ ...f, student_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select student…</option>
                  {students.map((s) => <option key={s.id} value={s.id}>{s.full_name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Research Title</label>
                <input value={form.research_title} onChange={(e) => setForm((f) => ({ ...f, research_title: e.target.value }))} placeholder="Thesis / Research title"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Research Area</label>
                <input value={form.research_area} onChange={(e) => setForm((f) => ({ ...f, research_area: e.target.value }))} placeholder="Plant Breeding, Animal Nutrition…"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setShowCreate(false)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              <button onClick={() => createCommittee.mutate()} disabled={createCommittee.isPending || !form.student_id}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                {createCommittee.isPending ? "Creating…" : "Create"}
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
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-4 bg-gray-50 rounded-xl p-4">
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Roll No</p><p className="text-sm text-gray-800 font-mono">{selected.student_roll ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Degree</p><p className="text-sm text-gray-800">{selected.program_name ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Department</p><p className="text-sm text-gray-800">{selected.department_name ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Research Area</p><p className="text-sm text-gray-800">{selected.research_area ?? "—"}</p></div>
                    <div><p className="text-xs font-semibold text-gray-500 uppercase">Status</p><span className={`inline-block px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[selected.status] ?? "bg-gray-100"}`}>{selected.status}</span></div>
                  </div>

                  {/* Committee Members */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="text-base font-bold text-gray-900">Committee Members</h4>
                      {selected.can_manage && !selected.is_locked && (
                        <button onClick={() => setShowAddMember(true)}
                          className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                          <Plus size={14} /> Add Member
                        </button>
                      )}
                    </div>
                    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
                      {selected.members.length === 0 ? (
                        <p className="text-sm text-gray-600 text-center py-6">No members yet.</p>
                      ) : (
                        <table className="w-full text-sm">
                          <thead className="bg-gray-50 border-b border-gray-200">
                            <tr>{["Name", "Designation", "Department", "Advisory Role", "Status"].map((h) => (
                              <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
                            ))}</tr>
                          </thead>
                          <tbody>
                            {selected.members.map((m, i) => {
                              const isMine = currentUser?.id === m.faculty_id;
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
                                    {m.accepted === false && <span className="text-red-500 text-sm font-semibold">Declined</span>}
                                    {m.accepted === null && isMine && (
                                      <div className="flex gap-1.5">
                                        <button onClick={() => acceptMembership.mutate({ committeeId: selected.id, memberId: m.id, accepted: true })}
                                          className="p-1 text-green-600 hover:bg-green-50 rounded"><CheckCircle2 size={16} /></button>
                                        <button onClick={() => acceptMembership.mutate({ committeeId: selected.id, memberId: m.id, accepted: false })}
                                          className="p-1 text-red-500 hover:bg-red-50 rounded"><XCircle size={16} /></button>
                                      </div>
                                    )}
                                    {m.accepted === null && !isMine && <span className="text-amber-600 text-sm font-semibold">Pending</span>}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>

                  {canLock && !selected.is_locked && (
                    <div className="flex justify-end">
                      <button onClick={() => setConfirm({
                        action: () => lockCommittee.mutate(selected.id),
                        title: "Lock Committee",
                        message: `Are you sure you want to lock the advisory committee for ${selected.student_name}? This cannot be undone.`,
                        confirmLabel: "Yes, Lock",
                        confirmClassName: "bg-amber-600 hover:bg-amber-700 text-white",
                      })}
                        className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold text-amber-700 border border-amber-200 rounded-xl hover:bg-amber-50">
                        <Lock size={14} /> Lock Committee
                      </button>
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
            {!canLookupUsers ? (
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
                    {facultyOptions.map((f) => <option key={f.id} value={f.id}>{f.full_name}{f.designation ? ` — ${f.designation}` : ""}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Role</label>
                  <div className="flex gap-2">
                    {["major_advisor", "co_major_advisor", "member"].map((r) => (
                      <button key={r} type="button" onClick={() => setMemberForm((f) => ({ ...f, role: r }))}
                        className={`flex-1 py-2 text-xs rounded-xl font-semibold border-2 transition-all ${memberForm.role === r ? "border-[#0D6E6E] bg-[#0D6E6E] text-white" : "border-gray-200 text-gray-600"}`}>
                        {committeeRoleLabel(r)}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}
            <div className="flex gap-3 mt-5">
              <button onClick={() => { setShowAddMember(false); setMemberForm({ faculty_id: "", role: "member" }); }}
                className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              {canLookupUsers && (
                <button onClick={() => addMember.mutate()} disabled={addMember.isPending || !memberForm.faculty_id}
                  className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                  {addMember.isPending ? "Adding…" : "Add Member"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
