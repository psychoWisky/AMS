"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ShieldCheck, Plus, Pencil, Loader2, Building2, GraduationCap, School, Lock, BadgeCheck, Trash2, Info } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface DepartmentRow { id: string; name: string; code: string; stream: string | null; is_active: boolean; }
// Programme<->Department many-to-many redesign — a Programme no longer
// carries a single department_id; associations are managed separately (see
// the Associations modal below) via ams_program_departments.
interface ProgramRow { id: string; name: string; code: string; level: string; duration_years: number; is_active: boolean; }
interface ProgramDepartmentLink { association_id: string; department_id: string; department_name: string; department_code: string; }
interface DepartmentProgramLink { association_id: string; program_id: string; program_name: string; program_code: string; program_level: string; }
interface CollegeRow { id: string; name: string; code: string; is_active: boolean; }
interface DesignationRow { id: string; name: string; is_active: boolean; created_at: string; }
interface RoleRow { id: string; code: string; name: string; is_system: boolean; is_active: boolean; user_count: number; }

const TABS = ["departments", "programmes", "colleges", "designations", "roles"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = { departments: "Departments", programmes: "Programmes", colleges: "Colleges", designations: "Designations", roles: "Roles" };

export default function AdminPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("departments");
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string } | null>(null);

  // ── Departments ──────────────────────────────────────────────────────────
  const [showDeptForm, setShowDeptForm] = useState(false);
  const [editDept, setEditDept] = useState<DepartmentRow | null>(null);
  const [deptForm, setDeptForm] = useState({ name: "", code: "", stream: "" });

  const { data: departments = [], isLoading: deptLoading } = useQuery<DepartmentRow[]>({
    queryKey: ["ams-admin-departments"],
    queryFn: async () => (await api.get("/departments", { params: { include_inactive: true } })).data,
    enabled: tab === "departments" || tab === "programmes",
  });

  const saveDept = useMutation({
    mutationFn: () => editDept
      ? api.put(`/departments/${editDept.id}`, deptForm)
      : api.post("/departments", deptForm),
    onSuccess: () => { toast.success(editDept ? "Department updated." : "Department created."); qc.invalidateQueries({ queryKey: ["ams-admin-departments"] }); closeDeptForm(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const toggleDeptActive = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) => api.put(`/departments/${id}`, { is_active }),
    onSuccess: () => { toast.success("Department status updated."); qc.invalidateQueries({ queryKey: ["ams-admin-departments"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  function closeDeptForm() { setShowDeptForm(false); setEditDept(null); setDeptForm({ name: "", code: "", stream: "" }); }
  function openEditDept(d: DepartmentRow) { setEditDept(d); setDeptForm({ name: d.name, code: d.code, stream: d.stream ?? "" }); setShowDeptForm(true); }

  // ── Programmes ───────────────────────────────────────────────────────────
  const [showProgForm, setShowProgForm] = useState(false);
  const [editProg, setEditProg] = useState<ProgramRow | null>(null);
  const [progForm, setProgForm] = useState({ name: "", code: "", level: "UG", duration_years: "4" });

  const { data: programmes = [], isLoading: progLoading } = useQuery<ProgramRow[]>({
    queryKey: ["ams-admin-programmes"],
    queryFn: async () => (await api.get("/departments/programs", { params: { include_inactive: true } })).data,
    enabled: tab === "programmes",
  });

  const saveProg = useMutation({
    mutationFn: () => editProg
      ? api.put(`/departments/programs/${editProg.id}`, { ...progForm, duration_years: parseInt(progForm.duration_years) })
      : api.post("/departments/programs", { ...progForm, duration_years: parseInt(progForm.duration_years) }),
    onSuccess: () => { toast.success(editProg ? "Programme updated." : "Programme created."); qc.invalidateQueries({ queryKey: ["ams-admin-programmes"] }); closeProgForm(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const toggleProgActive = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) => api.put(`/departments/programs/${id}`, { is_active }),
    onSuccess: () => { toast.success("Programme status updated."); qc.invalidateQueries({ queryKey: ["ams-admin-programmes"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  function closeProgForm() { setShowProgForm(false); setEditProg(null); setProgForm({ name: "", code: "", level: "UG", duration_years: "4" }); }
  function openEditProg(p: ProgramRow) { setEditProg(p); setProgForm({ name: p.name, code: p.code, level: p.level, duration_years: String(p.duration_years) }); setShowProgForm(true); }

  // ── Programme <-> Department associations (many-to-many redesign) ─────────
  // Manageable from either side: a Programme row's "Departments" button opens
  // this with type="program" (checklist = all Departments); a Department
  // row's "Programmes" button opens it with type="department" (checklist =
  // all Programmes). Both toggle the SAME ams_program_departments rows via
  // the same add/remove endpoints — removing a checkbox only removes the
  // association, never the Programme/Department/any User/Course referencing
  // either.
  const [assocFor, setAssocFor] = useState<{ type: "program" | "department"; id: string; name: string } | null>(null);

  const assocQuery = useQuery<(ProgramDepartmentLink | DepartmentProgramLink)[]>({
    queryKey: ["ams-associations", assocFor?.type, assocFor?.id],
    queryFn: async () => {
      const url = assocFor!.type === "program"
        ? `/departments/programs/${assocFor!.id}/departments`
        : `/departments/${assocFor!.id}/programs`;
      return (await api.get(url)).data;
    },
    enabled: !!assocFor,
  });
  const linkedIds = new Set(
    (assocQuery.data ?? []).map((r) => assocFor?.type === "program" ? (r as ProgramDepartmentLink).department_id : (r as DepartmentProgramLink).program_id)
  );

  const toggleAssoc = useMutation({
    mutationFn: ({ programId, departmentId, linked }: { programId: string; departmentId: string; linked: boolean }) =>
      linked
        ? api.delete(`/departments/programs/${programId}/departments/${departmentId}`)
        : api.post(`/departments/programs/${programId}/departments`, { department_id: departmentId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ams-associations", assocFor?.type, assocFor?.id] }),
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to update association."),
  });

  // ── Colleges ─────────────────────────────────────────────────────────────
  const [showCollegeForm, setShowCollegeForm] = useState(false);
  const [editCollege, setEditCollege] = useState<CollegeRow | null>(null);
  const [collegeForm, setCollegeForm] = useState({ name: "", code: "" });

  const { data: colleges = [], isLoading: collegeLoading } = useQuery<CollegeRow[]>({
    queryKey: ["ams-admin-colleges"],
    queryFn: async () => (await api.get("/admin/colleges", { params: { include_inactive: true } })).data,
    enabled: tab === "colleges",
  });

  const saveCollege = useMutation({
    mutationFn: async () => {
      if (editCollege) {
        return api.put(`/admin/colleges/${editCollege.id}`, {
          name: collegeForm.name,
        });
      }

      return api.post("/admin/colleges", collegeForm);
    },
    onSuccess: () => {
      toast.success(editCollege ? "College updated." : "College created.");
      qc.invalidateQueries({ queryKey: ["ams-admin-colleges"] });
      closeCollegeForm();
    },
    onError: (e: unknown) =>
      toast.error(
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          "Failed."
      ),
  });

  const deactivateCollege = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/colleges/${id}`),
    onSuccess: () => {
      toast.success("College deactivated.");
      qc.invalidateQueries({ queryKey: ["ams-admin-colleges"] });
    },
    onError: (e: unknown) =>
      toast.error(
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          "Failed."
      ),
  });

  const activateCollege = useMutation({
    mutationFn: (id: string) => api.put(`/admin/colleges/${id}`, { is_active: true }),
    onSuccess: () => {
      toast.success("College activated.");
      qc.invalidateQueries({ queryKey: ["ams-admin-colleges"] });
    },
    onError: (e: unknown) =>
      toast.error(
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          "Failed."
      ),
  });

  function closeCollegeForm() { setShowCollegeForm(false); setEditCollege(null); setCollegeForm({ name: "", code: "" }); }
  function openEditCollege(c: CollegeRow) { setEditCollege(c); setCollegeForm({ name: c.name, code: c.code }); setShowCollegeForm(true); }

  // ── Designations ─────────────────────────────────────────────────────────
  const [showDesigForm, setShowDesigForm] = useState(false);
  const [editDesig, setEditDesig] = useState<DesignationRow | null>(null);
  const [desigForm, setDesigForm] = useState({ name: "" });

  const { data: designations = [], isLoading: desigLoading } = useQuery<DesignationRow[]>({
    queryKey: ["ams-admin-designations"],
    queryFn: async () => (await api.get("/admin/designations")).data,
    enabled: tab === "designations",
  });

  const saveDesignation = useMutation({
    mutationFn: () => editDesig
      ? api.patch(`/admin/designations/${editDesig.id}`, { name: desigForm.name })
      : api.post("/admin/designations", desigForm),
    onSuccess: () => { toast.success(editDesig ? "Designation updated." : "Designation created."); qc.invalidateQueries({ queryKey: ["ams-admin-designations"] }); closeDesigForm(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const toggleDesigActive = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) => api.patch(`/admin/designations/${id}`, { is_active }),
    onSuccess: () => { toast.success("Designation status updated."); qc.invalidateQueries({ queryKey: ["ams-admin-designations"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  function closeDesigForm() { setShowDesigForm(false); setEditDesig(null); setDesigForm({ name: "" }); }
  function openEditDesig(d: DesignationRow) { setEditDesig(d); setDesigForm({ name: d.name }); setShowDesigForm(true); }

  // ── Roles ────────────────────────────────────────────────────────────────
  // MASTER DATA ONLY (role-management task) — a "custom" (non-system) role
  // created here is a catalog entry only. It cannot be selected as an actual
  // user's role and grants no system access until a real permission model is
  // built — the banner below states this explicitly so nobody is misled.
  const [showRoleForm, setShowRoleForm] = useState(false);
  const [editRole, setEditRole] = useState<RoleRow | null>(null);
  const [roleForm, setRoleForm] = useState({ code: "", name: "" });

  const { data: roles = [], isLoading: rolesLoading } = useQuery<RoleRow[]>({
    queryKey: ["ams-admin-roles"],
    queryFn: async () => (await api.get("/admin/roles")).data,
    enabled: tab === "roles",
  });

  const saveRole = useMutation({
    mutationFn: async () => {
      if (editRole) {
        return api.patch(
          `/admin/roles/${editRole.id}`,
          editRole.is_system
            ? { name: roleForm.name }
            : roleForm
        );
      }

      return api.post("/admin/roles", roleForm);
    },
    onSuccess: () => {
      toast.success(editRole ? "Role updated." : "Role created.");
      qc.invalidateQueries({ queryKey: ["ams-admin-roles"] });
      closeRoleForm();
    },
    onError: (e: unknown) =>
      toast.error(
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          "Failed."
      ),
  });

  const toggleRoleActive = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) => api.patch(`/admin/roles/${id}`, { is_active }),
    onSuccess: () => { toast.success("Role status updated."); qc.invalidateQueries({ queryKey: ["ams-admin-roles"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const deleteRole = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/roles/${id}`),
    onSuccess: () => { toast.success("Role deleted."); qc.invalidateQueries({ queryKey: ["ams-admin-roles"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to delete role."),
  });

  function closeRoleForm() { setShowRoleForm(false); setEditRole(null); setRoleForm({ code: "", name: "" }); }
  function openEditRole(r: RoleRow) { setEditRole(r); setRoleForm({ code: r.code, name: r.name }); setShowRoleForm(true); }

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ShieldCheck size={24} className="text-[#0D6E6E]" />Administration</h1>
        <p className="text-gray-700 text-base mt-1">Master data — roles, departments, programmes, colleges and designations</p>
      </div>

      <div className="flex gap-2 mb-5">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 rounded-xl text-sm font-semibold transition-colors ${tab === t ? "bg-[#0D6E6E] text-white" : "text-gray-600 hover:bg-gray-100"}`}>
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {/* ── Departments tab ─────────────────────────────────────────────── */}
      {tab === "departments" && (
        <>
          <div className="flex justify-end mb-3">
            <button onClick={() => setShowDeptForm(true)} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-sm hover:bg-[#178F8F]"><Plus size={15} /> Add Department</button>
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {deptLoading ? <div className="flex justify-center py-12"><Loader2 className="animate-spin text-gray-600" /></div> : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Name", "Code", "Stream", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {departments.map((d, i) => (
                    <tr key={d.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium flex items-center gap-2"><Building2 size={14} className="text-gray-400" />{d.name}</td>
                      <td className="px-4 py-3 font-mono text-[#0D6E6E]">{d.code}</td>
                      <td className="px-4 py-3 text-gray-600">{d.stream ?? "—"}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${d.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{d.is_active ? "Active" : "Inactive"}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1.5">
                          <button onClick={() => openEditDept(d)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Edit"><Pencil size={15} /></button>
                          <button onClick={() => setAssocFor({ type: "department", id: d.id, name: d.name })}
                            className="text-xs font-semibold px-2 py-1 rounded-lg text-[#0D6E6E] hover:bg-[#E6F4F4]">Programmes</button>
                          <button onClick={() => setConfirm({ action: () => toggleDeptActive.mutate({ id: d.id, is_active: !d.is_active }), title: d.is_active ? "Deactivate Department" : "Activate Department", message: `${d.is_active ? "Deactivate" : "Activate"} ${d.name}? Existing courses/users referencing it are not affected.` })}
                            className={`text-xs font-semibold px-2 py-1 rounded-lg ${d.is_active ? "text-red-600 hover:bg-red-50" : "text-green-700 hover:bg-green-50"}`}>{d.is_active ? "Deactivate" : "Activate"}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* ── Programmes tab ──────────────────────────────────────────────── */}
      {tab === "programmes" && (
        <>
          <div className="flex justify-end mb-3">
            <button onClick={() => setShowProgForm(true)} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-sm hover:bg-[#178F8F]"><Plus size={15} /> Add Programme</button>
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {progLoading ? <div className="flex justify-center py-12"><Loader2 className="animate-spin text-gray-600" /></div> : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Name", "Code", "Level", "Duration", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {programmes.map((p, i) => (
                    <tr key={p.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium flex items-center gap-2"><GraduationCap size={14} className="text-gray-400" />{p.name}</td>
                      <td className="px-4 py-3 font-mono text-[#0D6E6E]">{p.code}</td>
                      <td className="px-4 py-3 text-gray-600">{p.level}</td>
                      <td className="px-4 py-3 text-gray-600">{p.duration_years} yrs</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${p.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{p.is_active ? "Active" : "Inactive"}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1.5">
                          <button onClick={() => openEditProg(p)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Edit"><Pencil size={15} /></button>
                          <button onClick={() => setAssocFor({ type: "program", id: p.id, name: p.name })}
                            className="text-xs font-semibold px-2 py-1 rounded-lg text-[#0D6E6E] hover:bg-[#E6F4F4]">Departments</button>
                          <button onClick={() => setConfirm({ action: () => toggleProgActive.mutate({ id: p.id, is_active: !p.is_active }), title: p.is_active ? "Deactivate Programme" : "Activate Programme", message: `${p.is_active ? "Deactivate" : "Activate"} ${p.name}? Existing students/courses referencing it are not affected.` })}
                            className={`text-xs font-semibold px-2 py-1 rounded-lg ${p.is_active ? "text-red-600 hover:bg-red-50" : "text-green-700 hover:bg-green-50"}`}>{p.is_active ? "Deactivate" : "Activate"}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* ── Programme <-> Department associations modal ─────────────────── */}
      {assocFor && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6 max-h-[80vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">{assocFor.type === "program" ? "Associated Departments" : "Associated Programmes"}</h3>
            <p className="text-sm text-gray-600 mb-4">{assocFor.name}</p>
            {assocQuery.isLoading ? (
              <div className="flex justify-center py-8"><Loader2 className="animate-spin text-gray-600" /></div>
            ) : (
              <div className="space-y-1.5">
                {(assocFor.type === "program" ? departments : programmes).map((item) => {
                  const linked = linkedIds.has(item.id);
                  const programId = assocFor.type === "program" ? assocFor.id : item.id;
                  const departmentId = assocFor.type === "program" ? item.id : assocFor.id;
                  return (
                    <label key={item.id} className="flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-gray-50 cursor-pointer">
                      <input type="checkbox" checked={linked} disabled={toggleAssoc.isPending}
                        onChange={() => toggleAssoc.mutate({ programId, departmentId, linked })}
                        className="w-4 h-4 accent-[#0D6E6E]" />
                      <span className="text-sm text-gray-800">{item.name}</span>
                    </label>
                  );
                })}
              </div>
            )}
            <button onClick={() => setAssocFor(null)} className="w-full mt-5 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Close</button>
          </div>
        </div>
      )}

      {/* ── Colleges tab ────────────────────────────────────────────────── */}
      {tab === "colleges" && (
        <>
          <div className="flex justify-end mb-3">
            <button onClick={() => setShowCollegeForm(true)} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-sm hover:bg-[#178F8F]"><Plus size={15} /> Add College</button>
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {collegeLoading ? <div className="flex justify-center py-12"><Loader2 className="animate-spin text-gray-600" /></div> : colleges.length === 0 ? (
              <div className="text-center py-16 text-gray-600"><School size={40} className="mx-auto mb-3 opacity-30" /><p>No colleges added yet.</p></div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Name", "Code", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {colleges.map((c, i) => (
                    <tr key={c.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium flex items-center gap-2"><School size={14} className="text-gray-400" />{c.name}</td>
                      <td className="px-4 py-3 font-mono text-[#0D6E6E]">{c.code}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${c.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{c.is_active ? "Active" : "Inactive"}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1.5">
                          <button onClick={() => openEditCollege(c)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Edit"><Pencil size={15} /></button>
                          {c.is_active ? (
                            <button onClick={() => setConfirm({ action: () => deactivateCollege.mutate(c.id), title: "Deactivate College", message: `Deactivate ${c.name}? This is a soft delete — it can be reactivated later.` })}
                              className="text-xs font-semibold px-2 py-1 rounded-lg text-red-600 hover:bg-red-50">Deactivate</button>
                          ) : (
                            <button onClick={() => setConfirm({ action: () => activateCollege.mutate(c.id), title: "Activate College", message: `Activate ${c.name}?` })}
                              className="text-xs font-semibold px-2 py-1 rounded-lg text-green-700 hover:bg-green-50">Activate</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* ── Designations tab ────────────────────────────────────────────── */}
      {tab === "designations" && (
        <>
          <div className="flex justify-end mb-3">
            <button onClick={() => setShowDesigForm(true)} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-sm hover:bg-[#178F8F]"><Plus size={15} /> Add Designation</button>
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {desigLoading ? <div className="flex justify-center py-12"><Loader2 className="animate-spin text-gray-600" /></div> : designations.length === 0 ? (
              <div className="text-center py-16 text-gray-600"><BadgeCheck size={40} className="mx-auto mb-3 opacity-30" /><p>No designations added yet.</p></div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Name", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {designations.map((d, i) => (
                    <tr key={d.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium flex items-center gap-2"><BadgeCheck size={14} className="text-gray-400" />{d.name}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${d.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{d.is_active ? "Active" : "Inactive"}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1.5">
                          <button onClick={() => openEditDesig(d)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Edit"><Pencil size={15} /></button>
                          <button onClick={() => setConfirm({ action: () => toggleDesigActive.mutate({ id: d.id, is_active: !d.is_active }), title: d.is_active ? "Deactivate Designation" : "Activate Designation", message: `${d.is_active ? "Deactivate" : "Activate"} ${d.name}? Existing faculty records using this designation are not affected — it only ${d.is_active ? "disappears from" : "reappears in"} the Add Faculty selection list.` })}
                            className={`text-xs font-semibold px-2 py-1 rounded-lg ${d.is_active ? "text-red-600 hover:bg-red-50" : "text-green-700 hover:bg-green-50"}`}>{d.is_active ? "Deactivate" : "Activate"}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* ── Roles tab ────────────────────────────────────────────────────── */}
      {tab === "roles" && (
        <>
          <div className="flex items-start gap-2 mb-3 text-sm text-gray-700 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2.5">
            <Info size={15} className="text-amber-600 shrink-0 mt-0.5" />
            <span>Custom roles are currently master-data entries only. They do not grant system permissions or change application access until role permissions are implemented. The 8 system roles below (marked "System") are what actually control access and cannot be renamed to a different code, changed to custom, or deleted.</span>
          </div>
          <div className="flex justify-end mb-3">
            <button onClick={() => setShowRoleForm(true)} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-sm hover:bg-[#178F8F]"><Plus size={15} /> Add Role</button>
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {rolesLoading ? <div className="flex justify-center py-12"><Loader2 className="animate-spin text-gray-600" /></div> : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Name", "Code", "Type", "Status", "Users", "Action"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {roles.map((r, i) => (
                    <tr key={r.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium">{r.name}</td>
                      <td className="px-4 py-3 font-mono text-[#0D6E6E]">{r.code}</td>
                      <td className="px-4 py-3">
                        {r.is_system ? (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-blue-50 text-blue-700 rounded-full text-xs font-semibold"><Lock size={11} /> System</span>
                        ) : (
                          <span className="px-2 py-0.5 bg-purple-50 text-purple-700 rounded-full text-xs font-semibold">Custom</span>
                        )}
                      </td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${r.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{r.is_active ? "Active" : "Inactive"}</span></td>
                      <td className="px-4 py-3 text-gray-600">{r.user_count}</td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1.5">
                          <button onClick={() => openEditRole(r)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Edit"><Pencil size={15} /></button>
                          <button onClick={() => setConfirm({ action: () => toggleRoleActive.mutate({ id: r.id, is_active: !r.is_active }), title: r.is_active ? "Deactivate Role" : "Activate Role", message: `${r.is_active ? "Deactivate" : "Activate"} ${r.name}?` })}
                            className={`text-xs font-semibold px-2 py-1 rounded-lg ${r.is_active ? "text-red-600 hover:bg-red-50" : "text-green-700 hover:bg-green-50"}`}>{r.is_active ? "Deactivate" : "Activate"}</button>
                          {!r.is_system && (
                            <button onClick={() => setConfirm({ action: () => deleteRole.mutate(r.id), title: "Delete Role", message: r.user_count > 0 ? `${r.name} has ${r.user_count} user(s) and cannot be deleted.` : `Permanently delete ${r.name}? This cannot be undone.` })}
                              disabled={r.user_count > 0} className="p-1.5 text-red-500 hover:bg-red-50 rounded-lg disabled:opacity-30 disabled:hover:bg-transparent" title={r.user_count > 0 ? "Has users assigned" : "Delete"}><Trash2 size={15} /></button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* Department form modal */}
      {showDeptForm && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-4">{editDept ? "Edit Department" : "Add Department"}</h3>
            <div className="space-y-3">
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Name *</label><input value={deptForm.name} onChange={(e) => setDeptForm((f) => ({ ...f, name: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Code *</label><input value={deptForm.code} onChange={(e) => setDeptForm((f) => ({ ...f, code: e.target.value }))} disabled={!!editDept} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50" /></div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Stream</label><input value={deptForm.stream} onChange={(e) => setDeptForm((f) => ({ ...f, stream: e.target.value }))} placeholder="e.g. fisheries / veterinary" className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeDeptForm} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => { if (!deptForm.name || !deptForm.code) { toast.error("Name and Code are required."); return; } saveDept.mutate(); }} disabled={saveDept.isPending} className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">{saveDept.isPending ? "Saving…" : editDept ? "Save Changes" : "Create"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Programme form modal */}
      {showProgForm && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-4">{editProg ? "Edit Programme" : "Add Programme"}</h3>
            <div className="space-y-3">
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Name *</label><input value={progForm.name} onChange={(e) => setProgForm((f) => ({ ...f, name: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Code *</label><input value={progForm.code} onChange={(e) => setProgForm((f) => ({ ...f, code: e.target.value }))} disabled={!!editProg} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50" /></div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Level</label>
                <select value={progForm.level} onChange={(e) => setProgForm((f) => ({ ...f, level: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  {["UG", "PG", "PhD"].map((l) => <option key={l} value={l}>{l}</option>)}
                </select>
              </div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Duration (years)</label><input type="number" min={1} max={7} value={progForm.duration_years} onChange={(e) => setProgForm((f) => ({ ...f, duration_years: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
              {!editProg && (
                <p className="text-xs text-gray-500">Departments can be associated with this Programme afterward, from the Programmes list.</p>
              )}
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeProgForm} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => { if (!progForm.name || !progForm.code) { toast.error("Name and Code are required."); return; } saveProg.mutate(); }} disabled={saveProg.isPending} className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">{saveProg.isPending ? "Saving…" : editProg ? "Save Changes" : "Create"}</button>
            </div>
          </div>
        </div>
      )}

      {/* College form modal */}
      {showCollegeForm && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-4">{editCollege ? "Edit College" : "Add College"}</h3>
            <div className="space-y-3">
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Name *</label><input value={collegeForm.name} onChange={(e) => setCollegeForm((f) => ({ ...f, name: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Code *</label><input value={collegeForm.code} onChange={(e) => setCollegeForm((f) => ({ ...f, code: e.target.value }))} disabled={!!editCollege} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50" /></div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeCollegeForm} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => { if (!collegeForm.name || !collegeForm.code) { toast.error("Name and Code are required."); return; } saveCollege.mutate(); }} disabled={saveCollege.isPending} className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">{saveCollege.isPending ? "Saving…" : editCollege ? "Save Changes" : "Create"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Designation form modal */}
      {showDesigForm && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-4">{editDesig ? "Edit Designation" : "Add Designation"}</h3>
            <div className="space-y-3">
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Name *</label><input value={desigForm.name} onChange={(e) => setDesigForm({ name: e.target.value })} placeholder="e.g. Professor" className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeDesigForm} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => { if (!desigForm.name.trim()) { toast.error("Name is required."); return; } saveDesignation.mutate(); }} disabled={saveDesignation.isPending} className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">{saveDesignation.isPending ? "Saving…" : editDesig ? "Save Changes" : "Create"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Role form modal */}
      {showRoleForm && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h3 className="text-xl font-bold mb-1">{editRole ? "Edit Role" : "Add Role"}</h3>
            {!editRole && (
              <p className="text-sm text-gray-600 mb-4">This creates a master-data entry only — it will not grant any system access (see notice above).</p>
            )}
            {editRole?.is_system && (
              <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2 mb-4">This is a system role — only its display name can be changed.</p>
            )}
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Code *</label>
                <input value={roleForm.code} onChange={(e) => setRoleForm((f) => ({ ...f, code: e.target.value }))}
                  disabled={!!editRole?.is_system} placeholder="e.g. LAB_ASSISTANT"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base font-mono focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Name *</label>
                <input value={roleForm.name} onChange={(e) => setRoleForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Lab Assistant"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeRoleForm} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => { if (!roleForm.name.trim() || (!editRole && !roleForm.code.trim())) { toast.error("Code and Name are required."); return; } saveRole.mutate(); }} disabled={saveRole.isPending} className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">{saveRole.isPending ? "Saving…" : editRole ? "Save Changes" : "Create"}</button>
            </div>
          </div>
        </div>
      )}

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel="Yes, Confirm" onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
