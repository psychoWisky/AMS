from fastapi import APIRouter
from app.api.v1.endpoints import (
    auth, academic_calendar, courses, enrollment, grading,
    research, notifications, departments, admit_card, admission,
    academic_progress,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(departments.router)
api_router.include_router(academic_calendar.router)
api_router.include_router(courses.router)
api_router.include_router(enrollment.router)
api_router.include_router(grading.router)
api_router.include_router(research.router)
api_router.include_router(notifications.router)
api_router.include_router(admit_card.router)
api_router.include_router(admission.router)
api_router.include_router(academic_progress.router)
