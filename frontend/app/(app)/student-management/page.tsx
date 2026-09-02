"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useUser, useSetUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { IdCard, Loader2, KeyRound } from "lucide-react";

// Student Self-Service profile editing (BUSINESS_LOGIC.md K.1). Only the
// confirmed student-editable fields are collected here — Batch Year, Degree
// Name, Roll No., and Email are shown read-only and never sent in the update.
export default function StudentManagementPage() {
  const user = useUser();
  if (!user) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;
  // Keying by id (re-mounts only on account switch) lets the form's initial
  // state be derived once from `user` via useState's lazy initializer,
  // instead of syncing it in an effect.
  return <ProfileForm key={user.id} user={user} />;
}

function ProfileForm({ user }: { user: NonNullable<ReturnType<typeof useUser>> }) {
  const setUser = useSetUser();
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(() => {
    const [first, ...rest] = (user.full_name ?? "").split(" ");
    return {
      first_name: first ?? "", last_name: rest.join(" "),
      date_of_birth: user.date_of_birth ?? "", gender: user.gender ?? "",
      blood_group: user.blood_group ?? "", mobile: user.mobile ?? "", father_name: user.father_name ?? "",
      abc_id: user.abc_id ?? "", address: user.address ?? "",
    };
  });
  const [pwForm, setPwForm] = useState({ current_password: "", new_password: "" });

  const updateProfile = useMutation({
    mutationFn: () => api.patch("/auth/me", {
      first_name: form.first_name || undefined,
      last_name: form.last_name || undefined,
      date_of_birth: form.date_of_birth || undefined,
      gender: form.gender || undefined,
      blood_group: form.blood_group || undefined,
      mobile: form.mobile || undefined,
      father_name: form.father_name || undefined,
      abc_id: form.abc_id || undefined,
      address: form.address || undefined,
    }),
    onSuccess: (res) => {
      toast.success("Profile updated.");
      setUser(res.data);
      setEditing(false);
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to update profile."),
  });

  const changePassword = useMutation({
    mutationFn: () => api.post("/auth/change-password", pwForm),
    onSuccess: () => {
      toast.success("Password changed.");
      setPwForm({ current_password: "", new_password: "" });
      setUser({ ...user, must_change_password: false });
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to change password."),
  });

  const field = (label: string, key: keyof typeof form, type = "text") => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
      <input type={type} value={form[key]} disabled={!editing}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50 disabled:text-gray-500" />
    </div>
  );

  // BUSINESS_LOGIC.md Section N — Gender and Blood Group are dropdowns; Blood
  // Group remains optional (its dropdown includes a blank "not specified" option).
  const selectField = (label: string, key: "gender" | "blood_group", options: string[], required: boolean) => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}{required ? " *" : ""}</label>
      <select value={form[key]} disabled={!editing}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50 disabled:text-gray-500">
        <option value="">{required ? "Select…" : "Not specified"}</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><IdCard size={24} className="text-[#0D6E6E]" />Student Management</h1>
        <p className="text-gray-700 text-base mt-1">View and update your student profile</p>
      </div>

      {!user.profile_complete && (
        <div className="bg-amber-50 border border-amber-300 text-amber-900 rounded-2xl p-4 text-sm">
          Your profile is incomplete. Please fill in: <span className="font-semibold">{user.missing_profile_fields.join(", ")}</span> before continuing.
          <span className="block text-amber-700 mt-1">(Blood Group and ABC ID are optional and do not block completion.)</span>
        </div>
      )}

      <div className="bg-white rounded-2xl border border-gray-200 p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold text-gray-800">Profile</h2>
          <label className="flex items-center gap-2 text-sm font-semibold text-gray-700">
            <input type="checkbox" checked={editing} onChange={(e) => setEditing(e.target.checked)} />
            Enable Editing
          </label>
        </div>

        {/* Read-only fields */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-5 text-sm">
          <div><p className="text-gray-500">Roll No.</p><p className="font-semibold font-mono">{user.student_roll ?? "—"}</p></div>
          <div><p className="text-gray-500">Email</p><p className="font-semibold truncate">{user.email}</p></div>
          <div><p className="text-gray-500">Role</p><p className="font-semibold capitalize">{user.role}</p></div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {field("First Name", "first_name")}
          {field("Last Name", "last_name")}
          {field("Date of Birth", "date_of_birth", "date")}
          {selectField("Gender", "gender", ["Male", "Female", "Other"], true)}
          {selectField("Blood Group", "blood_group", ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"], false)}
          {field("Mobile No.", "mobile")}
          {field("Father's Name", "father_name")}
          {field("ABC ID", "abc_id")}
        </div>
        <div className="mt-4">
          <label className="block text-base font-semibold text-gray-700 mb-1">Address</label>
          <textarea value={form.address} disabled={!editing} rows={3}
            onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))}
            className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50 disabled:text-gray-500 resize-none" />
        </div>

        {editing && (
          <button onClick={() => updateProfile.mutate()} disabled={updateProfile.isPending}
            className="mt-5 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
            {updateProfile.isPending ? "Saving…" : "Update"}
          </button>
        )}
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 p-5">
        <h2 className="font-bold text-gray-800 mb-1 flex items-center gap-2"><KeyRound size={16} />Change Password</h2>
        {user.must_change_password && (
          <p className="text-sm text-amber-700 mb-3">You are using a temporary password — please change it.</p>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-3">
          <div>
            <label className="block text-base font-semibold text-gray-700 mb-1">Current Password</label>
            <input type="password" value={pwForm.current_password}
              onChange={(e) => setPwForm((f) => ({ ...f, current_password: e.target.value }))}
              className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
          </div>
          <div>
            <label className="block text-base font-semibold text-gray-700 mb-1">New Password</label>
            <input type="password" value={pwForm.new_password}
              onChange={(e) => setPwForm((f) => ({ ...f, new_password: e.target.value }))}
              className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
          </div>
        </div>
        <button onClick={() => changePassword.mutate()} disabled={changePassword.isPending || !pwForm.current_password || pwForm.new_password.length < 8}
          className="mt-4 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
          {changePassword.isPending ? "Changing…" : "Change Password"}
        </button>
      </div>
    </div>
  );
}
