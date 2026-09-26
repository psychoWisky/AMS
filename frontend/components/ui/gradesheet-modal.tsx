"use client";
import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";
import { api } from "@/services/api";
import { GRADESHEET_TYPE_LABELS, apiErrorMessage, type ComponentDef, type OfferingInfo, type SheetRow } from "@/components/ui/gradesheet-parts";

const THEORY_COMPONENTS = [
  { code: "first_test", label: "First Test" },
  { code: "mid_term", label: "Mid Term" },
  { code: "end_term", label: "End Term" },
];

interface Props {
  mode: "create" | "edit";
  offering: Pick<OfferingInfo, "id" | "course_number" | "credit">;
  students?: { id: string; name: string; roll_no: string | null }[];
  sheets?: SheetRow[];
  sheetId?: string;
  initial?: { total_theory_marks: number; theory_pass_marks: number; total_practical_marks: number; practical_pass_marks: number; components: ComponentDef[] };
  onClose: () => void;
  onSaved: (sheetId: string) => void;
}

const cents = (v: string) => Math.round((Number(v) || 0) * 100);

// Generate Gradesheet (create) / Edit structure (edit). Course number and credit come from the
// selected offering (never typed by the user); the backend re-validates every rule below.
export function GradesheetStructureModal({ mode, offering, students = [], sheets = [], sheetId, initial, onClose, onSaved }: Props) {
  const initComp = (code: string) => initial?.components.find((c) => c.code === code);
  const [type, setType] = useState("new");
  const [relatedId, setRelatedId] = useState("");
  const [theoryTotal, setTheoryTotal] = useState(initial ? String(initial.total_theory_marks || "") : "");
  const [theoryPass, setTheoryPass] = useState(initial ? String(initial.theory_pass_marks || "") : "");
  const [picked, setPicked] = useState<Record<string, { on: boolean; max: string }>>(
    Object.fromEntries(THEORY_COMPONENTS.map((c) => [c.code, { on: !!initComp(c.code), max: initComp(c.code) ? String(initComp(c.code)!.max_marks) : "" }])),
  );
  const [practicalTotal, setPracticalTotal] = useState(initial ? String(initial.total_practical_marks || "") : "");
  const [practicalPass, setPracticalPass] = useState(initial ? String(initial.practical_pass_marks || "") : "");
  const [subset, setSubset] = useState<Set<string>>(new Set(students.map((s) => s.id)));

  const selectedSum = useMemo(() => THEORY_COMPONENTS.reduce((n, c) => n + (picked[c.code].on ? cents(picked[c.code].max) : 0), 0), [picked]);
  const anySelected = THEORY_COMPONENTS.some((c) => picked[c.code].on);
  const theoryOk = cents(theoryTotal) === 0 ? !anySelected : anySelected && selectedSum === cents(theoryTotal);
  const marksOk = cents(theoryTotal) > 0 || cents(practicalTotal) > 0;
  const passOk = cents(theoryPass) <= cents(theoryTotal) && cents(practicalPass) <= cents(practicalTotal);
  const subsetOk = mode === "edit" || type === "new" || subset.size > 0;
  const valid = theoryOk && marksOk && passOk && subsetOk;

  const save = useMutation({
    mutationFn: async () => {
      const structure = {
        total_theory_marks: Number(theoryTotal) || 0, theory_pass_marks: Number(theoryPass) || 0,
        theory_components: THEORY_COMPONENTS.filter((c) => picked[c.code].on).map((c) => ({ code: c.code, max_marks: Number(picked[c.code].max) || 0 })),
        total_practical_marks: Number(practicalTotal) || 0, practical_pass_marks: Number(practicalPass) || 0,
      };
      if (mode === "edit") {
        await api.put(`/grading/sheets/${sheetId}/structure`, structure);
        return sheetId!;
      }
      const res = await api.post("/grading/sheets", {
        offering_id: offering.id, gradesheet_type: type, ...structure,
        ...(type !== "new" && relatedId ? { related_sheet_id: relatedId } : {}),
        ...(type !== "new" && subset.size < students.length ? { student_ids: [...subset] } : {}),
      });
      return res.data.id as string;
    },
    onSuccess: (id) => { toast.success(mode === "edit" ? "Gradesheet structure updated." : "Gradesheet created."); onSaved(id); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save the gradesheet.")),
  });

  const input = "w-full border border-gray-200 rounded-lg px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]";

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[92vh] overflow-y-auto">
        <div className="px-6 py-4 border-b border-gray-100">
          <h3 className="text-xl font-bold text-gray-900">{mode === "create" ? "Generate Gradesheet" : "Edit Gradesheet Structure"}</h3>
        </div>
        <div className="px-6 py-5 space-y-5">
          <div className="grid grid-cols-2 gap-4">
            <div><label className="block text-sm font-semibold text-gray-700 mb-1">Course Number</label><input readOnly value={offering.course_number} className={`${input} bg-gray-50`} /></div>
            <div><label className="block text-sm font-semibold text-gray-700 mb-1">Credit Hours</label><input readOnly value={offering.credit} className={`${input} bg-gray-50`} /></div>
          </div>

          {mode === "create" && (
            <div>
              <label className="block text-sm font-semibold text-gray-700 mb-1">Gradesheet Type</label>
              <div className="flex flex-wrap gap-4">
                {Object.entries(GRADESHEET_TYPE_LABELS).map(([k, label]) => (
                  <label key={k} className="flex items-center gap-2 text-base"><input type="radio" name="gs-type" checked={type === k} onChange={() => setType(k)} />{label} Gradesheet</label>
                ))}
              </div>
              {type !== "new" && (
                <div className="mt-3 space-y-3">
                  <div>
                    <label className="block text-sm font-semibold text-gray-700 mb-1">Related gradesheet (optional)</label>
                    <select value={relatedId} onChange={(e) => setRelatedId(e.target.value)} className={input}>
                      <option value="">— none —</option>
                      {sheets.map((s) => <option key={s.id} value={s.id}>{GRADESHEET_TYPE_LABELS[s.gradesheet_type] ?? s.gradesheet_type} · {s.teacher ?? "—"} · {s.status}</option>)}
                    </select>
                  </div>
                  {students.length > 0 && (
                    <div>
                      <p className="text-sm font-semibold text-gray-700 mb-1">Students included ({subset.size}/{students.length})</p>
                      <div className="max-h-36 overflow-y-auto border border-gray-200 rounded-lg p-2 space-y-1">
                        {students.map((s) => (
                          <label key={s.id} className="flex items-center gap-2 text-sm">
                            <input type="checkbox" checked={subset.has(s.id)} onChange={(e) => setSubset((prev) => { const n = new Set(prev); if (e.target.checked) n.add(s.id); else n.delete(s.id); return n; })} />
                            <span className="font-mono">{s.roll_no ?? "—"}</span> {s.name}
                          </label>
                        ))}
                      </div>
                      {!subsetOk && <p className="text-sm text-red-600 mt-1">Select at least one student.</p>}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="border border-gray-200 rounded-xl p-4 space-y-3">
            <h4 className="font-bold text-gray-900">Theory</h4>
            <div className="grid grid-cols-2 gap-4">
              <div><label className="block text-sm font-semibold text-gray-700 mb-1">Total Theory Marks</label><input type="number" min={0} step="0.01" value={theoryTotal} onChange={(e) => setTheoryTotal(e.target.value)} className={input} /></div>
              <div><label className="block text-sm font-semibold text-gray-700 mb-1">Theory Pass Marks</label><input type="number" min={0} step="0.01" value={theoryPass} onChange={(e) => setTheoryPass(e.target.value)} className={input} /></div>
            </div>
            <div className="space-y-2">
              <p className="text-sm font-semibold text-gray-700">Assessment Components</p>
              {THEORY_COMPONENTS.map((c) => (
                <div key={c.code} className="flex items-center gap-3">
                  <label className="flex items-center gap-2 w-40 text-base">
                    <input type="checkbox" checked={picked[c.code].on} onChange={(e) => setPicked((p) => ({ ...p, [c.code]: { ...p[c.code], on: e.target.checked } }))} />{c.label}
                  </label>
                  {picked[c.code].on && (
                    <input type="number" min={0} step="0.01" placeholder="Enter Total Marks" value={picked[c.code].max}
                      onChange={(e) => setPicked((p) => ({ ...p, [c.code]: { ...p[c.code], max: e.target.value } }))} className={`${input} max-w-[12rem]`} />
                  )}
                </div>
              ))}
              {(anySelected || cents(theoryTotal) > 0) && (
                <p className={`text-sm font-semibold ${theoryOk ? "text-green-700" : "text-red-600"}`}>
                  Selected components total {selectedSum / 100} of {cents(theoryTotal) / 100} Total Theory Marks{theoryOk ? " ✓" : " — they must be equal."}
                </p>
              )}
            </div>
          </div>

          <div className="border border-gray-200 rounded-xl p-4 space-y-3">
            <h4 className="font-bold text-gray-900">Practical</h4>
            <div className="grid grid-cols-2 gap-4">
              <div><label className="block text-sm font-semibold text-gray-700 mb-1">Total Practical Marks</label><input type="number" min={0} step="0.01" value={practicalTotal} onChange={(e) => setPracticalTotal(e.target.value)} className={input} /></div>
              <div><label className="block text-sm font-semibold text-gray-700 mb-1">Practical Pass Marks</label><input type="number" min={0} step="0.01" value={practicalPass} onChange={(e) => setPracticalPass(e.target.value)} className={input} /></div>
            </div>
          </div>
          {!marksOk && <p className="text-sm text-red-600">Configure Total Theory Marks and/or Total Practical Marks.</p>}
          {!passOk && <p className="text-sm text-red-600">Pass marks cannot exceed the corresponding total.</p>}
        </div>
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-gray-100">
          <button onClick={onClose} disabled={save.isPending} className="px-5 py-2.5 border border-gray-200 rounded-xl font-semibold text-gray-700 hover:bg-gray-50">Close</button>
          <button onClick={() => save.mutate()} disabled={!valid || save.isPending}
            className="px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#0a5858] disabled:opacity-50 inline-flex items-center gap-2">
            {save.isPending && <Loader2 size={16} className="animate-spin" />}Save Changes
          </button>
        </div>
      </div>
    </div>
  );
}
