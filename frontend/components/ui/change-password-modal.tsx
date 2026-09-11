"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { KeyRound, Eye, EyeOff } from "lucide-react";

// Shared by two flows (this task's confirmed requirement — one component,
// not duplicated password-form logic):
//   - Self-service: any signed-in user changes their OWN password. Posts to
//     the existing session-authorized /auth/change-password endpoint. No
//     current-password field — the user's JWT session already authorizes
//     this action.
//   - Admin reset: Super Admin / Academic Admin resets ANOTHER user's
//     password without ever seeing or being asked for their existing one.
//     Posts to /auth/users/{id}/reset-password. UI text makes clear this is
//     an administrative reset, not the admin "knowing" the old password.
interface ChangePasswordModalProps {
  mode: "self" | "admin-reset";
  targetUserId?: string;
  targetUserName?: string;
  onClose: () => void;
  /** Called after a successful change, before onClose — e.g. to update
   *  cached user state (must_change_password) for the "self" mode caller. */
  onSuccess?: () => void;
}

function PasswordField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const [show, setShow] = useState(false);
  return (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
      <div className="relative">
        <input
          type={show ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full border border-gray-300 rounded-xl px-3 py-2 pr-10 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]"
        />
        <button type="button" onClick={() => setShow((s) => !s)} tabIndex={-1}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-700">
          {show ? <EyeOff size={16} /> : <Eye size={16} />}
        </button>
      </div>
    </div>
  );
}

export function ChangePasswordModal({ mode, targetUserId, targetUserName, onClose, onSuccess }: ChangePasswordModalProps) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  const isAdminReset = mode === "admin-reset";

  const mutation = useMutation({
    mutationFn: () =>
      isAdminReset
        ? api.post(`/auth/users/${targetUserId}/reset-password`, { new_password: newPassword, confirm_password: confirmPassword })
        : api.post("/auth/change-password", { new_password: newPassword, confirm_password: confirmPassword }),
    onSuccess: () => {
      toast.success(isAdminReset ? "Password reset." : "Password changed.");
      onSuccess?.();
      onClose();
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to change password."),
  });

  const mismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;
  const canSubmit = newPassword.length >= 8 && newPassword === confirmPassword && !mutation.isPending;

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
        <div className="flex items-center gap-3 mb-1">
          <div className="w-10 h-10 rounded-full bg-[#E6F4F4] flex items-center justify-center flex-shrink-0">
            <KeyRound size={20} className="text-[#0D6E6E]" />
          </div>
          <h3 className="text-lg font-bold text-gray-900">{isAdminReset ? "Reset Password" : "Change Password"}</h3>
        </div>
        {isAdminReset ? (
          <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2 my-3">
            Administrative reset for <span className="font-semibold">{targetUserName}</span>. You will not see their existing password.
            They will be required to change it on next login.
          </p>
        ) : (
          <p className="text-sm text-gray-600 mb-4 mt-1">Choose a new password for your account.</p>
        )}
        <div className="space-y-3">
          <PasswordField label="New Password" value={newPassword} onChange={setNewPassword} />
          <PasswordField label="Confirm New Password" value={confirmPassword} onChange={setConfirmPassword} />
          {mismatch && <p className="text-xs text-red-600">Passwords do not match.</p>}
          {newPassword.length > 0 && newPassword.length < 8 && <p className="text-xs text-amber-600">Password must be at least 8 characters.</p>}
        </div>
        <div className="flex gap-3 mt-5">
          <button onClick={onClose} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium">Cancel</button>
          <button onClick={() => mutation.mutate()} disabled={!canSubmit}
            className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold disabled:opacity-60">
            {mutation.isPending ? "Saving…" : isAdminReset ? "Reset Password" : "Change Password"}
          </button>
        </div>
      </div>
    </div>
  );
}
