"use client";
import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { toast } from "sonner";
import { Users, Plus, Search, Loader2, Pencil, KeyRound, Upload, ShieldCheck } from "lucide-react";
import { ROLES, ADMIN_ROLES } from "@/lib/utils";
import { ChangePasswordModal } from "@/components/ui/change-password-modal";
import { UserBulkUploadModal } from "@/components/ui/user-bulk-upload-modal";

interface User {
  id: string; email: string; full_name: string; role: string; designation: string | null;
  department_id: string | null; program_id: string | null;
  first_name?: string; middle_name?: string | null; last_name?: string; mobile?: string | null;
  department_name?: string | null;
  title?: string | null; employee_id?: string | null; date_of_birth?: string | null; gender?: string | null;
  blood_group?: string | null; father_name?: string | null; abc_id?: string | null; address?: string | null;
  college_id?: string | null;
  // Every persisted role assignment, role + department kept as a pair (see
  // GET /auth/users). `role`/`department_name` above are only the user's
  // legacy primary role / home department.
  assigned_role_assignments?: RoleAssignmentRow[];
}
interface DepartmentOpt { id: string; name: string; code: string; }
// Multi-role/multi-department task (this revision) — one persisted
// UserRoleAssignment row, exactly as GET /auth/users/{id}/roles returns it.
interface RoleAssignmentRow { id: string; role: string; department_id: string | null; department_name: string | null; }
// Programme<->Department many-to-many redesign — Program no longer carries a
// single department_id (see backend GET /departments/programs); Department
// options for a chosen Programme come from GET /departments?program_id=...
interface ProgramOpt { id: string; name: string; code: string; level: string; }

// Students are not managed here (see the Students page), so they are not a role option.
const ROLE_OPTIONS = Object.keys(ROLES).filter((r) => r !== "student");
const TITLES = ["Dr.", "Mr", "Mrs", "Miss"];
const GENDERS = ["Male", "Female", "Other"];
const BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"];
// Profile fields the Edit dialog sends only when the admin actually changed them.
const PROFILE_EDIT_FIELDS = ["title", "employee_id", "date_of_birth", "gender", "blood_group", "father_name", "abc_id", "address", "college_id"] as const;
// One "Role — Department" line per real assignment; the pair is never split
// into separate role / department lists. Institution-wide roles have no
// department and read "Role — Global"; a Student has none either (their
// department lives on the user record) and reads plain.
function assignmentLabel(a: RoleAssignmentRow): string {
  const roleLabel = ROLES[a.role as keyof typeof ROLES] ?? a.role;
  if (a.department_name) return `${roleLabel} — ${a.department_name}`;
  return a.role === "student" ? roleLabel : `${roleLabel} — Global`;
}

// Multi-role/multi-department task (this revision) — the Assigned Roles
// modal's two sections: institution-wide roles (plain checkboxes, no
// department) vs. department-scoped roles (one checkbox per department,
// fully independent — checking HOD for a department never auto-checks
// FACULTY for it, or vice versa; each is its own explicit grant).
// `super_admin` is deliberately included here too (backend enforces its
// exclusivity — see auth.py's add_user_role — this UI does not need its
// own separate copy of that rule, it just surfaces whatever the backend
// rejects).
const GLOBAL_ROLE_OPTIONS = ["super_admin", "dpgs", "incharge_academic_cell", "student"];
const DEPARTMENT_ROLE_OPTIONS = ["hod", "faculty"];

// Issue 4 fix: a single named constant for the Add User form's blank state,
// reused every time the form must return to empty (opening Add User, a
// successful create, and Cancel) instead of relying on stale useState from a
// previous session of the modal.
const EMPTY_FORM = { email: "", password: "", first_name: "", middle_name: "", last_name: "", role: "faculty", designation: "", mobile: "", department_id: "", program_id: "" };
const EMPTY_EDIT_FORM = {
  email: "", first_name: "", middle_name: "", last_name: "", role: "", designation: "", mobile: "", department_id: "", program_id: "",
  title: "", employee_id: "", date_of_birth: "", gender: "", blood_group: "", father_name: "", abc_id: "", address: "", college_id: "",
};

export default function UsersPage() {
  const role = useRole();
  const isAdmin = role ? ADMIN_ROLES.includes(role) : false;
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editUser, setEditUser] = useState<User | null>(null);
  const [editForm, setEditForm] = useState(EMPTY_EDIT_FORM);
  const [editInitial, setEditInitial] = useState(EMPTY_EDIT_FORM);
  const [form, setForm] = useState(EMPTY_FORM);
  const [resetPwUser, setResetPwUser] = useState<User | null>(null);
  const [showBulkUpload, setShowBulkUpload] = useState(false);
  // Multi-role/role-switching task — Super Admin/Academic Admin role
  // assignment. Separate from the legacy single Role <select> above (still
  // used for the primary/display role on create/edit); this manages the
  // real ams_user_role_assignments rows the backend actually authorizes on.
  const [rolesUser, setRolesUser] = useState<User | null>(null);

  const { data: users = [], isLoading } = useQuery<User[]>({
    queryKey: ["ams-users"],
    queryFn: async () => (await api.get("/auth/users")).data,
  });

  // Full, unfiltered Department list — used whenever no Programme is
  // selected (Programme remains optional for Faculty/HOD; a Department may
  // still be picked on its own, exactly as before).
  const { data: departments = [] } = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
    enabled: showCreate || !!editUser || !!rolesUser,
  });

  const { data: colleges = [] } = useQuery<{ id: string; name: string }[]>({
    queryKey: ["ams-colleges-active"],
    queryFn: async () => (await api.get("/admin/colleges")).data,
    enabled: !!editUser,
  });

  const { data: programs = [] } = useQuery<ProgramOpt[]>({
    queryKey: ["ams-programs"],
    queryFn: async () => (await api.get("/departments/programs")).data,
    enabled: showCreate || !!editUser,
  });

  // Programme<->Department many-to-many redesign — Department options are
  // now filtered BY Programme (reverse of the old Department->Programme
  // direction), since one Department may belong to several Programmes.
  const createDeptQuery = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments-for-program", form.program_id],
    queryFn: async () => (await api.get("/departments", { params: { program_id: form.program_id } })).data,
    enabled: showCreate && !!form.program_id,
  });
  const editDeptQuery = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments-for-program", editForm.program_id],
    queryFn: async () => (await api.get("/departments", { params: { program_id: editForm.program_id } })).data,
    enabled: !!editUser && !!editForm.program_id,
  });
  const createDeptOptions = createDeptQuery.data ?? [];
  const editDeptOptions = editDeptQuery.data ?? [];
  const createDepartments = form.program_id ? createDeptOptions : departments;
  const editDepartments = editForm.program_id ? editDeptOptions : departments;

  // If the currently-selected Department is no longer valid for whichever
  // Programme is now selected, clear it rather than silently submitting an
  // invalid pair — the backend would reject it anyway, but this avoids a
  // round-trip error for the common "changed Programme" case. Guarded on
  // `isSuccess` so this never fires against the query's transient empty
  // default before its actual data has arrived (which would otherwise wipe
  // out a perfectly valid pre-existing selection, e.g. when opening Edit).
  useEffect(() => {
    if (createDeptQuery.isSuccess && form.program_id && form.department_id && !createDeptOptions.some((d) => d.id === form.department_id)) {
      setForm((f) => ({ ...f, department_id: "" }));
    }
  }, [createDeptQuery.isSuccess, createDeptOptions, form.program_id, form.department_id]);
  useEffect(() => {
    if (editDeptQuery.isSuccess && editForm.program_id && editForm.department_id && !editDeptOptions.some((d) => d.id === editForm.department_id)) {
      setEditForm((f) => ({ ...f, department_id: "" }));
    }
  }, [editDeptQuery.isSuccess, editDeptOptions, editForm.program_id, editForm.department_id]);

  const createUser = useMutation({
    mutationFn: () => api.post("/auth/users", {
      ...form,
      middle_name: form.middle_name || null,
      department_id: form.department_id || null,
      program_id: form.program_id || null,
    }),
    // Issue 4 fix: reset to EMPTY_FORM (not just close the modal) on success,
    // so the next time Add User is opened it can never show User A's data.
    onSuccess: () => { toast.success("User created."); qc.invalidateQueries({ queryKey: ["ams-users"] }); setShowCreate(false); setForm(EMPTY_FORM); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const updateUser = useMutation({
    mutationFn: () => {
      // Profile fields are sent only when changed; a blank value clears it (null).
      const changedProfile: Record<string, string | null> = {};
      for (const k of PROFILE_EDIT_FIELDS) if (editForm[k] !== editInitial[k]) changedProfile[k] = editForm[k] || null;
      return api.patch(`/auth/users/${editUser?.id}`, {
      ...changedProfile,
      email: editForm.email,
      first_name: editForm.first_name,
      middle_name: editForm.middle_name || null,
      last_name: editForm.last_name,
      designation: editForm.designation || null,
      mobile: editForm.mobile || null,
      role: editForm.role,
      department_id: editForm.department_id || null,
      program_id: editForm.program_id || null,
    });
    },
    onSuccess: () => { toast.success("User updated."); qc.invalidateQueries({ queryKey: ["ams-users"] }); setEditUser(null); setEditForm(EMPTY_EDIT_FORM); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed to update user."),
  });

  // Multi-role/multi-department task (this revision) — each assignment now
  // carries its own department (or null for institution-wide roles), and
  // the same role can appear more than once (e.g. HOD for two different
  // departments) — so the modal needs the full per-assignment list, not
  // just a flat set of role names.
  const rolesQuery = useQuery<{ user_id: string; assigned_roles: string[]; assignments: RoleAssignmentRow[] }>({
    queryKey: ["ams-user-roles", rolesUser?.id],
    queryFn: async () => (await api.get(`/auth/users/${rolesUser?.id}/roles`)).data,
    enabled: !!rolesUser,
  });
  const assignments = rolesQuery.data?.assignments ?? [];

  const addRole = useMutation({
    mutationFn: (body: { role: string; department_id?: string }) => api.post(`/auth/users/${rolesUser?.id}/roles`, body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["ams-user-roles", rolesUser?.id] }); qc.invalidateQueries({ queryKey: ["ams-users"] }); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Could not assign role."),
  });
  const removeRole = useMutation({
    mutationFn: ({ role, department_id }: { role: string; department_id?: string | null }) =>
      api.delete(`/auth/users/${rolesUser?.id}/roles/${role}`, { params: department_id ? { department_id } : undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["ams-user-roles", rolesUser?.id] }); qc.invalidateQueries({ queryKey: ["ams-users"] }); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Could not remove role."),
  });

  const filtered = users.filter((u) =>
    // A user matches a role filter if ANY of their assignments has that role
    // (the legacy primary `u.role` is only a fallback for a response without assignments).
    (!roleFilter || (u.assigned_role_assignments ? u.assigned_role_assignments.some((a) => a.role === roleFilter) : u.role === roleFilter)) &&
    (!search || u.full_name.toLowerCase().includes(search.toLowerCase()) || u.email.toLowerCase().includes(search.toLowerCase()))
  );

  // Issue 4 fix: Add User always opens onto a guaranteed-blank form,
  // regardless of any state left over from a prior Add or Edit session.
  const openCreate = () => { setForm(EMPTY_FORM); setShowCreate(true); };
  const closeCreate = () => { setShowCreate(false); setForm(EMPTY_FORM); };

  const openEdit = (u: User) => {
    const form = {
      email: u.email,
      first_name: u.first_name ?? u.full_name.split(" ")[0] ?? "",
      middle_name: u.middle_name ?? "",
      last_name: u.last_name ?? "",
      role: u.role,
      designation: u.designation ?? "",
      mobile: u.mobile ?? "",
      department_id: u.department_id ?? "",
      program_id: u.program_id ?? "",
      title: u.title ?? "",
      employee_id: u.employee_id ?? "",
      date_of_birth: u.date_of_birth ?? "",
      gender: u.gender ?? "",
      blood_group: u.blood_group ?? "",
      father_name: u.father_name ?? "",
      abc_id: u.abc_id ?? "",
      address: u.address ?? "",
      college_id: u.college_id ?? "",
    };
    setEditForm(form);
    setEditInitial(form);
    setEditUser(u);
  };
  const closeEdit = () => { setEditUser(null); setEditForm(EMPTY_EDIT_FORM); setEditInitial(EMPTY_EDIT_FORM); };
  // One labelled text/date input bound to a key of the Edit form.
  const editInput = (label: string, key: keyof typeof EMPTY_EDIT_FORM, type = "text") => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
      <input type={type} value={editForm[key]} onChange={(e) => setEditForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
    </div>
  );
  const editSelect = (label: string, key: keyof typeof EMPTY_EDIT_FORM, options: { value: string; label: string }[]) => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
      <select value={editForm[key]} onChange={(e) => setEditForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
        <option value="">None</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  );

  return (
    <div className="p-6 w-full">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><Users size={24} className="text-[#0D6E6E]" />User Management</h1>
          <p className="text-gray-700 text-base mt-1">Manage staff and system accounts — students are managed under Students</p>
        </div>
        <div className="flex items-center gap-2">
          {/* Bulk Faculty/User Excel Upload task (this revision) — Super
              Admin/Academic Admin-scoped; the backend independently enforces
              the same roles already authorized for individual Add User. */}
          <button onClick={() => setShowBulkUpload(true)}
            className="flex items-center gap-2 px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl font-semibold text-base hover:bg-[#E6F4F4]">
            <Upload size={16} /> Bulk Upload
          </button>
          <button onClick={openCreate}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Add User
          </button>
        </div>
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
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-4">Create New User</h3>
            <div className="space-y-3">
              {[["Email", "email", "email"], ["Password", "password", "password"], ["First Name", "first_name", "text"], ["Middle Name", "middle_name", "text"], ["Last Name", "last_name", "text"], ["Designation", "designation", "text"], ["Mobile", "mobile", "text"]].map(([label, key, type]) => (
                <div key={key}>
                  <label className="block text-base font-semibold text-gray-700 mb-1">{label}{key === "middle_name" && <span className="font-normal text-gray-500"> (optional)</span>}</label>
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
                <label className="block text-base font-semibold text-gray-700 mb-1">Programme <span className="font-normal text-gray-500">(optional)</span></label>
                <select value={form.program_id} onChange={(e) => setForm((f) => ({ ...f, program_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {programs.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.level})</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                <select value={form.department_id} onChange={(e) => setForm((f) => ({ ...f, department_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {createDepartments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
                {form.program_id && createDepartments.length === 0 && (
                  <p className="text-xs text-amber-600 mt-1">No Departments are associated with this Programme yet.</p>
                )}
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeCreate} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
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
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">Edit User</h3>
            <p className="text-sm text-gray-600 mb-4">{editUser.full_name} — {editUser.email}. Role assignments are managed separately (the shield icon).</p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">First Name</label>
                <input value={editForm.first_name} onChange={(e) => setEditForm((f) => ({ ...f, first_name: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Middle Name <span className="font-normal text-gray-500">(optional)</span></label>
                <input value={editForm.middle_name} onChange={(e) => setEditForm((f) => ({ ...f, middle_name: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Last Name</label>
                <input value={editForm.last_name} onChange={(e) => setEditForm((f) => ({ ...f, last_name: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Email</label>
                <input type="email" value={editForm.email} onChange={(e) => setEditForm((f) => ({ ...f, email: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Designation</label>
                <input value={editForm.designation} onChange={(e) => setEditForm((f) => ({ ...f, designation: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Mobile</label>
                <input value={editForm.mobile} onChange={(e) => setEditForm((f) => ({ ...f, mobile: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Role</label>
                <select value={editForm.role} onChange={(e) => setEditForm((f) => ({ ...f, role: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{ROLES[r as keyof typeof ROLES]}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Programme <span className="font-normal text-gray-500">(optional)</span></label>
                <select value={editForm.program_id} onChange={(e) => setEditForm((f) => ({ ...f, program_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {programs.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.level})</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                <select value={editForm.department_id} onChange={(e) => setEditForm((f) => ({ ...f, department_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">None</option>
                  {editDepartments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
                {editForm.program_id && editDepartments.length === 0 && (
                  <p className="text-xs text-amber-600 mt-1">No Departments are associated with this Programme yet.</p>
                )}
              </div>
              {editSelect("Title", "title", TITLES.map((t) => ({ value: t, label: t })))}
              {editInput("Employee ID", "employee_id")}
              {editInput("Date of Birth", "date_of_birth", "date")}
              {editSelect("Gender", "gender", GENDERS.map((g) => ({ value: g, label: g })))}
              {editSelect("Blood Group", "blood_group", BLOOD_GROUPS.map((g) => ({ value: g, label: g })))}
              {editInput("Father's Name", "father_name")}
              {editInput("ABC ID", "abc_id")}
              {editSelect("College", "college_id", colleges.map((c) => ({ value: c.id, label: c.name })))}
              <div className="md:col-span-2">
                <label className="block text-base font-semibold text-gray-700 mb-1">Address</label>
                <textarea rows={2} value={editForm.address} onChange={(e) => setEditForm((f) => ({ ...f, address: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] resize-none" />
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeEdit} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
              <button onClick={() => updateUser.mutate()} disabled={updateUser.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
                {updateUser.isPending ? "Saving…" : "Save Changes"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Reset Password modal (Issue 6 — administrative reset, no old password) */}
      {resetPwUser && (
        <ChangePasswordModal mode="admin-reset" targetUserId={resetPwUser.id} targetUserName={resetPwUser.full_name} onClose={() => setResetPwUser(null)} />
      )}

      {/* Multi-role/multi-department task (this revision, extending the
          earlier multi-role/role-switching task) — Assigned Roles
          management. Institution-wide roles (Super Admin/DPGS/Incharge
          Academic Cell/Student) are plain checkboxes; HOD/Faculty are
          checkbox GROUPS, one row per department — each (role, department)
          pair is its own explicit, independently-toggled grant. Checking
          HOD for a department never auto-checks Faculty for it, or vice
          versa; toggling calls the assignment endpoints directly (each
          change takes effect immediately — no "Save" step, mirroring
          ChangePasswordModal's single-action style). The backend
          independently enforces Super Admin-only access and every existing
          business rule (HOD/Faculty require a real department, Super Admin
          exclusivity, DPGS/Incharge single-holder, etc.) — this UI only
          surfaces whatever error it returns; it never decides validity
          itself. */}
      {rolesUser && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 max-h-[85vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">Assigned Roles</h3>
            <p className="text-sm text-gray-600 mb-4">{rolesUser.full_name} — {rolesUser.email}</p>
            {rolesQuery.isLoading ? (
              <div className="flex justify-center py-8"><Loader2 className="animate-spin text-gray-600" /></div>
            ) : (
              <div className="space-y-4">
                <div className="space-y-2">
                  {GLOBAL_ROLE_OPTIONS.map((r) => {
                    const checked = assignments.some((a) => a.role === r);
                    const busy = addRole.isPending || removeRole.isPending;
                    return (
                      <label key={r} className="flex items-center gap-2 text-base">
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={busy}
                          onChange={() => (checked ? removeRole.mutate({ role: r }) : addRole.mutate({ role: r }))}
                        />
                        {ROLES[r as keyof typeof ROLES] ?? r}
                      </label>
                    );
                  })}
                </div>
                {DEPARTMENT_ROLE_OPTIONS.map((r) => (
                  <div key={r}>
                    <p className="text-sm font-bold text-gray-800 mb-1.5">{ROLES[r as keyof typeof ROLES] ?? r}</p>
                    <div className="space-y-1.5 max-h-36 overflow-y-auto border border-gray-100 rounded-lg p-2">
                      {departments.map((d) => {
                        const checked = assignments.some((a) => a.role === r && a.department_id === d.id);
                        const busy = addRole.isPending || removeRole.isPending;
                        return (
                          <label key={d.id} className="flex items-center gap-2 text-sm">
                            <input
                              type="checkbox"
                              checked={checked}
                              disabled={busy}
                              onChange={() => (checked
                                ? removeRole.mutate({ role: r, department_id: d.id })
                                : addRole.mutate({ role: r, department_id: d.id }))}
                            />
                            {d.name}
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="flex gap-3 mt-5">
              <button onClick={() => setRolesUser(null)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Close</button>
            </div>
          </div>
        </div>
      )}

      {showBulkUpload && (
        <UserBulkUploadModal
          uploadUrl="/auth/users/bulk-upload"
          templateUrl="/auth/users/bulk-upload/template"
          templateFilename="ams_users_bulk_upload_template.xlsx"
          onClose={() => setShowBulkUpload(false)}
          onSuccess={(count, queued) => {
            toast.success(
              `${count} user${count === 1 ? "" : "s"} created successfully.` +
              (queued > 0 ? ` ${queued} credential email(s) queued for delivery.` : ""),
            );
            qc.invalidateQueries({ queryKey: ["ams-users"] });
            setShowBulkUpload(false);
          }}
        />
      )}

      {/* Users table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
        {isLoading ? <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div> : filtered.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><Users size={40} className="mx-auto mb-3 opacity-30" /><p>No users found.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Name", "Email", "Role", "Designation", "Department", "Assigned Roles", ...(isAdmin ? ["Action"] : [])].map((h) => (
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
                  <td className="px-4 py-3 text-gray-700">{u.department_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">
                    {(u.assigned_role_assignments ?? []).length === 0 ? "—" : (
                      <div className="flex flex-col gap-1">
                        {(u.assigned_role_assignments ?? []).map((a) => (
                          <span key={a.id}>{assignmentLabel(a)}</span>
                        ))}
                      </div>
                    )}
                  </td>
                  {isAdmin && (
                    <td className="px-4 py-3">
                      <div className="flex gap-1">
                        <button
                          onClick={() => openEdit(u)}
                          title="Edit user"
                          className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><Pencil size={16} /></button>
                        <button
                          onClick={() => setResetPwUser(u)}
                          title="Reset password"
                          className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><KeyRound size={16} /></button>
                        <button
                          onClick={() => setRolesUser(u)}
                          title="Manage assigned roles"
                          className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><ShieldCheck size={16} /></button>
                      </div>
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
