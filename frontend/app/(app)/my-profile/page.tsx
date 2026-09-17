"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useUser, useSetUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { IdCard, Loader2, KeyRound } from "lucide-react";
import { ChangePasswordModal } from "@/components/ui/change-password-modal";

// Bulk Faculty/User Excel Upload task (this revision) — the investigation
// found `PATCH /auth/me` was already role-agnostic on the backend (any
// authenticated user, not just students, can already update these fields),
// but no frontend page for any non-student role actually used it. This is
// the minimum new page filling that gap — mirrors
// student-management/page.tsx's established pattern (lazy-initialized form
// state keyed by user.id, PATCH /auth/me on save) WITHOUT modifying that
// page or its student-specific behavior at all. Only the fields Section 32
// explicitly named are exposed here (DOB/Gender/Blood Group/Father's Name/
// Address/ABC ID/Mobile) — Department/College/Designation/Role remain
// admin-only, read-only display, never editable by the user themselves.
export default function MyProfilePage() {
  const user = useUser();
  if (!user) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;
  return <ProfileForm key={user.id} user={user} />;
}

function ProfileForm({ user }: { user: NonNullable<ReturnType<typeof useUser>> }) {
  const setUser = useSetUser();
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(() => ({
    date_of_birth: user.date_of_birth ?? "", gender: user.gender ?? "",
    blood_group: user.blood_group ?? "", mobile: user.mobile ?? "", father_name: user.father_name ?? "",
    abc_id: user.abc_id ?? "", address: user.address ?? "",
  }));
  const [showChangePw, setShowChangePw] = useState(false);

  const updateProfile = useMutation({
    mutationFn: () => api.patch("/auth/me", {
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

  const field = (label: string, key: keyof typeof form, type = "text") => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
      <input type={type} value={form[key]} disabled={!editing}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50 disabled:text-gray-500" />
    </div>
  );

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
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><IdCard size={24} className="text-[#0D6E6E]" />My Profile</h1>
        <p className="text-gray-700 text-base mt-1">View your account details and complete any information not supplied when your account was created</p>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold text-gray-800">Profile</h2>
          <label className="flex items-center gap-2 text-sm font-semibold text-gray-700">
            <input type="checkbox" checked={editing} onChange={(e) => setEditing(e.target.checked)} />
            Enable Editing
          </label>
        </div>

        {/* Read-only, admin-managed fields — never editable here (Section 32's
            explicit "do not expose fields users should not edit themselves"). */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-5 text-sm">
          <div><p className="text-gray-500">Name</p><p className="font-semibold">{user.full_name}</p></div>
          <div><p className="text-gray-500">Email</p><p className="font-semibold break-all">{user.email}</p></div>
          <div><p className="text-gray-500">Active Role</p><p className="font-semibold capitalize">{(user.active_role ?? user.role)?.replace(/_/g, " ")}</p></div>
          <div><p className="text-gray-500">Designation</p><p className="font-semibold">{user.designation ?? "—"}</p></div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {field("Date of Birth", "date_of_birth", "date")}
          {selectField("Gender", "gender", ["Male", "Female", "Other"], false)}
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
        <p className="text-sm text-gray-600 mt-2 mb-4">Set a new password for your account.</p>
        <button onClick={() => setShowChangePw(true)}
          className="px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F]">
          Change Password
        </button>
      </div>

      {showChangePw && (
        <ChangePasswordModal
          mode="self"
          onClose={() => setShowChangePw(false)}
          onSuccess={() => setUser({ ...user, must_change_password: false })}
        />
      )}
    </div>
  );
}
