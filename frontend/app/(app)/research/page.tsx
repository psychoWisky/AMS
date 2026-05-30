"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { toast } from "sonner";
import { FlaskConical, Plus, Lock, Users, Loader2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface Committee { id: string; student_name: string; student_roll: string | null; research_title: string | null; research_area: string | null; status: string; is_locked: boolean; members: { id: string; faculty_name: string; role: string; accepted: boolean | null }[]; }
interface User { id: string; full_name: string; role: string; designation: string | null; }

const ROLE_BADGE: Record<string, string> = { major_advisor: "bg-blue-100 text-blue-700", co_major_advisor: "bg-purple-100 text-purple-700", member: "bg-gray-100 text-gray-700" };

export default function ResearchPage() {
  const role = useRole();
  const qc = useQueryClient();
  const isAdmin = ["super_admin","academic_admin","hod"].includes(role ?? "");
  const [showCreate, setShowCreate] = useState(false);
  const [selectedCommittee, setSelectedCommittee] = useState<Committee | null>(null);
  const [form, setForm] = useState({ student_id: "", research_title: "", research_area: "" });
  const [memberForm, setMemberForm] = useState({ faculty_id: "", role: "member" });
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

  const { data: committees = [], isLoading } = useQuery<Committee[]>({
    queryKey: ["ams-committees"],
    queryFn: async () => (await api.get("/research/committees")).data,
  });

  const { data: allUsers = [] } = useQuery<User[]>({
    queryKey: ["ams-users"],
    queryFn: async () => (await api.get("/auth/users")).data,
    enabled: isAdmin,
  });

  const students = allUsers.filter((u) => u.role === "student");
  const faculty = allUsers.filter((u) => ["faculty","hod","research_supervisor"].includes(u.role));

  const createCommittee = useMutation({
    mutationFn: () => api.post("/research/committees", form),
    onSuccess: () => { toast.success("Committee created."); qc.invalidateQueries({ queryKey: ["ams-committees"] }); setShowCreate(false); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const addMember = useMutation({
    mutationFn: () => api.post(`/research/committees/${selectedCommittee?.id}/members`, memberForm),
    onSuccess: async () => {
      toast.success("Member added.");
      qc.invalidateQueries({ queryKey: ["ams-committees"] });
      // Refresh selected committee
      const res = await api.get("/research/committees");
      const updated = res.data.find((c: Committee) => c.id === selectedCommittee?.id);
      if (updated) setSelectedCommittee(updated);
    },
  });

  const lockCommittee = useMutation({
    mutationFn: (id: string) => api.patch(`/research/committees/${id}/lock`),
    onSuccess: () => { toast.success("Committee locked."); qc.invalidateQueries({ queryKey: ["ams-committees"] }); setSelectedCommittee(null); },
  });

  if (isLoading) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><FlaskConical size={24} className="text-[#0D6E6E]" />PG / Research Management</h1>
          <p className="text-gray-700 text-base mt-1">Advisory committees for PG & PhD students</p>
        </div>
        {isAdmin && <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
          <Plus size={16} /> New Committee
        </button>}
      </div>

      {/* Create modal */}
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

      {/* Committee list */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {committees.length === 0 && (
          <div className="col-span-2 text-center py-16 text-gray-600 bg-white rounded-2xl border border-gray-200">
            <FlaskConical size={40} className="mx-auto mb-3 opacity-30" /><p>No advisory committees yet.</p>
          </div>
        )}
        {committees.map((c) => (
          <div key={c.id} className="bg-white rounded-2xl border border-gray-200 p-5 hover:shadow-md transition-all">
            <div className="flex items-start justify-between mb-3">
              <div>
                <p className="font-bold text-gray-900">{c.student_name}</p>
                {c.student_roll && <p className="text-sm text-gray-700 font-mono">{c.student_roll}</p>}
              </div>
              <div className="flex items-center gap-2">
                {c.is_locked && <Lock size={14} className="text-amber-600" />}
                <span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${c.status === "locked" ? "bg-amber-100 text-amber-700" : c.status === "active" ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{c.status}</span>
              </div>
            </div>
            {c.research_title && <p className="text-base font-medium text-gray-700 mb-1">{c.research_title}</p>}
            {c.research_area && <p className="text-sm text-gray-700 mb-3">{c.research_area}</p>}

            <div className="space-y-1.5 mb-3">
              {c.members.map((m) => (
                <div key={m.id} className="flex items-center gap-2">
                  <span className={`px-2 py-0.5 rounded text-sm font-semibold ${ROLE_BADGE[m.role] ?? "bg-gray-100 text-gray-600"}`}>{m.role.replace(/_/g, " ")}</span>
                  <span className="text-sm text-gray-700">{m.faculty_name}</span>
                  {m.accepted === true && <span className="text-sm text-green-600">✓ Accepted</span>}
                  {m.accepted === false && <span className="text-sm text-red-500">✗ Declined</span>}
                </div>
              ))}
              {c.members.length === 0 && <p className="text-sm text-gray-600">No members yet.</p>}
            </div>

            {isAdmin && !c.is_locked && (
              <div className="flex gap-2 pt-3 border-t border-gray-100">
                <button onClick={() => setSelectedCommittee(c)}
                  className="flex-1 py-2 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-xl hover:bg-[#E6F4F4]">
                  <Users size={12} className="inline mr-1" />Add Member
                </button>
                <button onClick={() => setConfirm({
                  action: () => lockCommittee.mutate(c.id),
                  title: "Lock Committee",
                  message: `Are you sure you want to lock the advisory committee for ${c.student_name}? This cannot be undone.`,
                  confirmLabel: "Yes, Lock",
                  confirmClassName: "bg-amber-600 hover:bg-amber-700 text-white",
                })} className="flex-1 py-2 text-sm font-semibold text-amber-700 border border-amber-200 rounded-xl hover:bg-amber-50">
                  <Lock size={12} className="inline mr-1" />Lock Committee
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}

      {/* Add member modal */}
      {selectedCommittee && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-4">Add Committee Member</h3>
            <p className="text-sm text-gray-600 mb-3">For: {selectedCommittee.student_name}</p>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Faculty</label>
                <select value={memberForm.faculty_id} onChange={(e) => setMemberForm((f) => ({ ...f, faculty_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select faculty…</option>
                  {faculty.map((f) => <option key={f.id} value={f.id}>{f.full_name} — {f.designation}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Role</label>
                <div className="flex gap-2">
                  {["major_advisor","co_major_advisor","member"].map((r) => (
                    <button key={r} type="button" onClick={() => setMemberForm((f) => ({ ...f, role: r }))}
                      className={`flex-1 py-2 text-sm rounded-xl font-semibold border-2 transition-all ${memberForm.role === r ? "border-[#0D6E6E] bg-[#0D6E6E] text-white" : "border-gray-200 text-gray-600"}`}>
                      {r.replace(/_/g, " ")}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setSelectedCommittee(null)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              <button onClick={() => addMember.mutate()} disabled={addMember.isPending || !memberForm.faculty_id}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                {addMember.isPending ? "Adding…" : "Add Member"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
