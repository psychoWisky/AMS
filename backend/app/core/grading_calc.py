"""Pure calculation helpers for the Gradesheet/Result workflow (no DB access).

All arithmetic is Decimal — never binary float — so GPA/CGPA are exact.

Confirmed AVFU rules implemented here
  * GPA  = Total Credit Points / Total Credits          (`compute_gpa`)
  * GPA is DISPLAYED to 3 decimals by truncation        (`format_gpa`): 166.550 / 20
    = 8.3275 is shown as 8.327 (the confirmed example; round-half-up/even would
    give 8.328, so the display truncates). The full-precision value is what is
    stored and what CGPA averages.
  * First semester: GPA only, no CGPA; from the second semester on,
    CGPA = arithmetic mean of the semester GPAs           (`compute_cgpa`)

NOT decided here (open AVFU questions, see BUSINESS_LOGIC.md): treatment of
non-credit courses, F/backlog courses, repeat/revised/make-up grades in GPA/CGPA.
The grade bands/grade-points themselves come from `models.grading.GRADE_SCALE`.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Iterable, Optional

from app.models.grading import compute_grade

_CENT = Decimal("0.01")
_GPA_STORAGE = Decimal("0.000001")
_GPA_DISPLAY = Decimal("0.001")


def to_decimal(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round2(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def compute_entry_outcome(
    components: Iterable[dict],
    marks_by_code: dict,
    *,
    theory_pass_marks: Decimal = Decimal("0"),
    practical_pass_marks: Decimal = Decimal("0"),
    is_absent: bool = False,
) -> dict:
    """Totals + grade for one student's entry.

    `components`: dicts with `code`, `component_type` ("theory"/"practical"),
    `max_marks`. `marks_by_code`: code -> Decimal|None.

    A grade is produced only once every configured component has marks (or the
    student is absent) — an incomplete entry gets totals-so-far but no grade.
    Absent -> grade F, 0 points (unchanged from the previous behaviour).

    Pass marks: when a theory/practical pass mark is configured (> 0) and the
    student's theory/practical total is below it, the grade is F. (The
    instructor configures pass marks explicitly in the Generate Gradesheet
    modal; treating a shortfall as F is the assumption recorded in
    BUSINESS_LOGIC.md.) The letter otherwise comes from the marks percentage.
    """
    comps = list(components)
    max_total = sum((to_decimal(c["max_marks"]) for c in comps), Decimal("0"))
    if is_absent:
        return {
            "theory_total": Decimal("0"), "practical_total": Decimal("0"), "grand_total": Decimal("0"),
            "max_total": max_total, "marks_percent": Decimal("0.00"),
            "grade_letter": "F", "grade_points": 0.0, "complete": True,
        }
    theory_total = Decimal("0")
    practical_total = Decimal("0")
    complete = True
    for c in comps:
        m = to_decimal(marks_by_code.get(c["code"]))
        if m is None:
            complete = False
            continue
        if c["component_type"] == "practical":
            practical_total += m
        else:
            theory_total += m
    grand_total = theory_total + practical_total
    percent = round2(grand_total / max_total * 100) if max_total > 0 else Decimal("0.00")
    letter = points = None
    if complete and comps:
        theory_fail = theory_pass_marks > 0 and theory_total < theory_pass_marks
        practical_fail = practical_pass_marks > 0 and practical_total < practical_pass_marks
        if theory_fail or practical_fail:
            letter, points = "F", 0.0
        else:
            letter, points = compute_grade(percent)
    return {
        "theory_total": theory_total, "practical_total": practical_total, "grand_total": grand_total,
        "max_total": max_total, "marks_percent": percent,
        "grade_letter": letter, "grade_points": points, "complete": complete and bool(comps),
    }


def compute_gpa(total_credit_points: Decimal, total_credits: Decimal) -> Optional[Decimal]:
    """GPA = Total Credit Points / Total Credits, full precision (truncated to
    6 dp for storage). None when there are no credits."""
    if total_credits is None or total_credits <= 0:
        return None
    return (total_credit_points / total_credits).quantize(_GPA_STORAGE, rounding=ROUND_DOWN)


def compute_cgpa(semester_gpas: list) -> Optional[Decimal]:
    """CGPA = average of semester GPAs; None for the first semester (a single
    GPA) — there is no CGPA until the second semester."""
    gpas = [g for g in semester_gpas if g is not None]
    if len(gpas) < 2:
        return None
    return (sum(gpas, Decimal("0")) / Decimal(len(gpas))).quantize(_GPA_STORAGE, rounding=ROUND_DOWN)


def format_gpa(value: Optional[Decimal]) -> Optional[str]:
    """3-decimal truncated display (8.3275 -> "8.327")."""
    if value is None:
        return None
    return str(value.quantize(_GPA_DISPLAY, rounding=ROUND_DOWN))


def attendance_band(percent) -> Optional[str]:
    """Confirmed colour convention: <75 red, 75-85 yellow, >85 green."""
    p = to_decimal(percent)
    if p is None:
        return None
    if p < 75:
        return "red"
    if p <= 85:
        return "yellow"
    return "green"
