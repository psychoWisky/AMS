"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { toast } from "sonner";
import { Users, Plus, Search, Loader2, Pencil } from "lucide-react";
import { ROLES, ADMIN_ROLES } from "@/lib/utils";

interface User { id: string; email: string; full_name: string; role: string; designation: string | null; department_id: string | null; program_id: string | null; }
interface DepartmentOpt { id: string; name: string; code: string; }
interface ProgramOpt { id: string; name: string; code: string; level: string; department_id: string; }

const ROLE_OPTIONS = Object.keys(ROLES);

export default function UsersPage() {
  const role = useRole();
  const isAdmin = role ? ADMIN_ROLES.includes(role) : false;
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editUser, setEditUser] = useState<User | null>(null);
  const [editForm, setEditForm] = useState({ role: "", department_id: "", program_id: "" });
  const [form, setForm] = useState({ email: "", password: "", first_name: "", last_name: "", role: "faculty", designation: "", mobile: "", department_id: "", program_id: "" });

  const { data: users = [], isLoading } = useQuery<User[]>({
    queryKey: ["ams-users"],
    queryFn: async () => (await api.get("/auth/users")).data,
  });

  const { data: departments = [] } = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
    enabled: showCreate || !!editUser,
  });

  const { data: programs = [] } = useQuery<ProgramOpt[]>({
    queryKey: ["ams-programs"],
    queryFn: async () => (await api.get("/departments/programs")).data,
    enabled: showCreate || !!editUser,
  });

  const createUser = useMutation({
    mutationFn: () => api.post("/auth/users", {
      ...form,
      department_id: form.department_id || null,
      program_id: form.program_id || null,
    }),
    onSuccess: () => { toast.success("User created."); qc.invalidateQueries({ queryKey: ["ams-users"] }); setShowCreate(false); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const updateUser = useMutation({
    mutationFn: () => api.patch(`/auth/users/${editUser?.id}`, {
      role: editForm.role,
      department_id: editForm.department_id || null,
      program_id: editForm.program_id || null,
    }),
    onSuccess: () => { toast.success("User updated."); qc.invalidateQueries({ queryKey: ["ams-users"] }); setEditUser(null); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed to update user."),
  });

  const filtered = users.filter((u) =>
    (!roleFilter || u.role === roleFilter) &&
    (!search || u.full_name.toLowerCase().includes(search.toLowerCase()) || u.email.toLowerCase().includes(search.toLowerCase()))
  );

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><Users size={24} className="text-[#0D6E6E]" />User Management</h1>
          <p className="text-gray-700 text-base mt-1">Manage faculty, students, and admin accounts</p>
        </div>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
          <Plus size={16} /> Add User
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-5">
        <div className="relative flex-1 max-w-xs">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search users…"
            className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
        </div>
        <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Roles</option>
          {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{ROLES[r as keyof typeof ROLES]}</option>)}
        </select>
      </div>

      {/* Create modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-4">Create New User</h3>
            <div className="space-y-3">
              {[["Email", "email", "email"], ["Password", "password", "password"], ["First Name", "first_name", "text"], ["Last Name", "last_name", "text"], ["Designation", "designation", "text"], ["Mobile", "mobile", "text"]].map(([label, key, type]) => (
                <div key={key}>
                  <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
                  <input type={type} value={(form as Record<string, string>)[key]}
                    onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              ))}
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Role</label>
                <select value={form.role} onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{ROLES[r as keyof typeof ROLES]}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                <select value={form.department_id} onChange={(e) => setForm((f) => ({ ...f, department_id: e.target.value, program_id: "" }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Program</label>
                <select value={form.program_id} onChange={(e) => setForm((f) => ({ ...f, program_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {(form.department_id ? programs.filter((p) => p.department_id === form.department_id) : programs)
                    .map((p) => <option key={p.id} value={p.id}>{p.name} ({p.level})</option>)}
                </select>
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setShowCreate(false)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              <button onClick={() => createUser.mutate()} disabled={createUser.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                {createUser.isPending ? "Creating…" : "Create User"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit modal */}
      {editUser && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-1">Edit User</h3>
            <p className="text-sm text-gray-600 mb-4">{editUser.full_name} — {editUser.email}</p>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Role</label>
                <select value={editForm.role} onChange={(e) => setEditForm((f) => ({ ...f, role: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{ROLES[r as keyof typeof ROLES]}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                <select value={editForm.department_id} onChange={(e) => setEditForm((f) => ({ ...f, department_id: e.target.value, program_id: "" }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Program</label>
                <select value={editForm.program_id} onChange={(e) => setEditForm((f) => ({ ...f, program_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {(editForm.department_id ? programs.filter((p) => p.department_id === editForm.department_id) : programs)
                    .map((p) => <option key={p.id} value={p.id}>{p.name} ({p.level})</option>)}
                </select>
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setEditUser(null)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              <button onClick={() => updateUser.mutate()} disabled={updateUser.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                {updateUser.isPending ? "Saving…" : "Save Changes"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Users table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
        {isLoading ? <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div> : filtered.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><Users size={40} className="mx-auto mb-3 opacity-30" /><p>No users found.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["Name", "Email", "Role", "Designation", ...(isAdmin ? ["Action"] : [])].map((h) => (
                <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {filtered.map((u, i) => (
                <tr key={u.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 font-medium">{u.full_name}</td>
                  <td className="px-4 py-3 text-gray-700">{u.email}</td>
                  <td className="px-4 py-3"><span className="px-2 py-0.5 bg-[#E6F4F4] text-[#0D6E6E] rounded text-sm font-semibold">{ROLES[u.role as keyof typeof ROLES] ?? u.role}</span></td>
                  <td className="px-4 py-3 text-gray-700">{u.designation ?? "—"}</td>
                  {isAdmin && (
                    <td className="px-4 py-3">
                      <button
                        onClick={() => { setEditUser(u); setEditForm({ role: u.role, department_id: u.department_id ?? "", program_id: u.program_id ?? "" }); }}
                        className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><Pencil size={16} /></button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
