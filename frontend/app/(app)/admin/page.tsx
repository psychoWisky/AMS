"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ShieldCheck, Plus, Pencil, Loader2, Building2, GraduationCap, School, Lock, BadgeCheck } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface DepartmentRow { id: string; name: string; code: string; stream: string | null; is_active: boolean; }
interface ProgramRow { id: string; name: string; code: string; level: string; department_id: string; duration_years: number; is_active: boolean; }
interface CollegeRow { id: string; name: string; code: string; is_active: boolean; }
interface DesignationRow { id: string; name: string; is_active: boolean; created_at: string; }
interface RoleRow { value: string; label: string; user_count: number; }

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
  const [progForm, setProgForm] = useState({ name: "", code: "", level: "UG", department_id: "", duration_years: "4" });

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

  function closeProgForm() { setShowProgForm(false); setEditProg(null); setProgForm({ name: "", code: "", level: "UG", department_id: "", duration_years: "4" }); }
  function openEditProg(p: ProgramRow) { setEditProg(p); setProgForm({ name: p.name, code: p.code, level: p.level, department_id: p.department_id, duration_years: String(p.duration_years) }); setShowProgForm(true); }

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
    mutationFn: () => editCollege
      ? api.put(`/admin/colleges/${editCollege.id}`, { name: collegeForm.name })
      : api.post("/admin/colleges", collegeForm),
    onSuccess: () => { toast.success(editCollege ? "College updated." : "College created."); qc.invalidateQueries({ queryKey: ["ams-admin-colleges"] }); closeCollegeForm(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const deactivateCollege = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/colleges/${id}`),
    onSuccess: () => { toast.success("College deactivated."); qc.invalidateQueries({ queryKey: ["ams-admin-colleges"] }); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
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

  // ── Roles (read-only) ────────────────────────────────────────────────────
  const { data: roles = [], isLoading: rolesLoading } = useQuery<RoleRow[]>({
    queryKey: ["ams-admin-roles"],
    queryFn: async () => (await api.get("/admin/roles")).data,
    enabled: tab === "roles",
  });

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
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Name", "Code", "Level", "Department", "Duration", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {programmes.map((p, i) => (
                    <tr key={p.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium flex items-center gap-2"><GraduationCap size={14} className="text-gray-400" />{p.name}</td>
                      <td className="px-4 py-3 font-mono text-[#0D6E6E]">{p.code}</td>
                      <td className="px-4 py-3 text-gray-600">{p.level}</td>
                      <td className="px-4 py-3 text-gray-600">{departments.find((d) => d.id === p.department_id)?.name ?? "—"}</td>
                      <td className="px-4 py-3 text-gray-600">{p.duration_years} yrs</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${p.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{p.is_active ? "Active" : "Inactive"}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1.5">
                          <button onClick={() => openEditProg(p)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Edit"><Pencil size={15} /></button>
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
                          {c.is_active && (
                            <button onClick={() => setConfirm({ action: () => deactivateCollege.mutate(c.id), title: "Deactivate College", message: `Deactivate ${c.name}? This is a soft delete — it can be reactivated later.` })}
                              className="text-xs font-semibold px-2 py-1 rounded-lg text-red-600 hover:bg-red-50">Deactivate</button>
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

      {/* ── Roles tab (read-only) ───────────────────────────────────────── */}
      {tab === "roles" && (
        <>
          <div className="flex items-center gap-2 mb-3 text-sm text-gray-600 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2">
            <Lock size={14} className="text-amber-600" />
            Roles are a fixed part of the application's authorization model and cannot be created, edited, or deleted from this screen. See BUSINESS_LOGIC.md Section N.5 for the reasoning.
          </div>
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {rolesLoading ? <div className="flex justify-center py-12"><Loader2 className="animate-spin text-gray-600" /></div> : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Role", "Active Users"].map((h) => <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>)}</tr></thead>
                <tbody>
                  {roles.map((r, i) => (
                    <tr key={r.value} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 font-medium">{r.label}</td>
                      <td className="px-4 py-3 text-gray-600">{r.user_count}</td>
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
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Department *</label>
                <select value={progForm.department_id} onChange={(e) => setProgForm((f) => ({ ...f, department_id: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select…</option>
                  {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
              </div>
              <div><label className="block text-base font-semibold text-gray-700 mb-1">Duration (years)</label><input type="number" min={1} max={7} value={progForm.duration_years} onChange={(e) => setProgForm((f) => ({ ...f, duration_years: e.target.value }))} className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" /></div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeProgForm} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => { if (!progForm.name || !progForm.code || !progForm.department_id) { toast.error("Name, Code and Department are required."); return; } saveProg.mutate(); }} disabled={saveProg.isPending} className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">{saveProg.isPending ? "Saving…" : editProg ? "Save Changes" : "Create"}</button>
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

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel="Yes, Confirm" onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
