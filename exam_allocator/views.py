from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie


from .models import (
    Allocation,
    AllocationSession,
    Class,
    Department,
    Exam,
    ExamRegistration,
    Room,
    Student,
    Subject,
    UploadedFile,
)
from .services.allocation_workflow import (
    AllocationWorkflowError,
    run_allocation,
)

from .parsers.student_parser import parse_student_excel
from .parsers.classroom_parser import parse_classroom_excel
from .parsers.timetable_parser import parse_timetable_excel

from .services.import_service import (
    import_students,
    import_classrooms,
    import_timetable,
)
from .services.registration_service import (
    create_all_exam_registrations,
)


@ensure_csrf_cookie
def session_list(request):

    sessions = AllocationSession.objects.order_by(
        "-updated_at",
        "-session_id",
    )

    return render(
        request,
        "exam_allocator/session_list.html",
        {
            "sessions": sessions,
        },
    )


def create_session(request):

    if request.method == "POST":

        name = request.POST.get("name", "").strip()

        if not name:
            return render(
                request,
                "exam_allocator/create_session.html",
                {
                    "error": "Please enter a session name.",
                },
            )

        session = AllocationSession.objects.create(
            name=name,
        )

        return redirect(
            "exam_allocator:session_detail",
            session_id=session.session_id,
        )

    return render(
        request,
        "exam_allocator/create_session.html",
    )


def session_detail(request, session_id):

    session = get_object_or_404(
        AllocationSession,
        session_id=session_id,
    )

    departments_count = session.departments.count()

    classes_count = Class.objects.filter(department__session=session).count()

    students_count = Student.objects.filter(
        student_class__department__session=session
    ).count()

    subjects_count = session.subjects.count()

    exams_count = Exam.objects.filter(subject__session=session).count()

    rooms_count = session.rooms.count()

    registrations_count = ExamRegistration.objects.filter(
        exam__subject__session=session
    ).count()

    allocations_count = Allocation.objects.filter(
        exam__subject__session=session
    ).count()

    return render(
        request,
        "exam_allocator/session_detail.html",
        {
            "session": session,
            "departments_count": departments_count,
            "classes_count": classes_count,
            "students_count": students_count,
            "subjects_count": subjects_count,
            "exams_count": exams_count,
            "rooms_count": rooms_count,
            "registrations_count": registrations_count,
            "allocations_count": allocations_count,
        },
    )


def exam_list(request, session_id=None):

    if session_id is not None:
        session = get_object_or_404(
            AllocationSession,
            session_id=session_id,
        )

        exams = (
            Exam.objects.select_related("subject")
            .filter(subject__session=session)
            .order_by(
                "exam_date",
                "session",
                "exam_id",
            )
        )

    else:
        session = None

        exams = Exam.objects.select_related("subject").order_by(
            "exam_date",
            "session",
            "exam_id",
        )

    return render(
        request,
        "exam_allocator/exam_list.html",
        {
            "exams": exams,
            "session": session,
        },
    )


def generate_registrations(request, session_id):

    session = get_object_or_404(
        AllocationSession,
        session_id=session_id,
    )

    if request.method != "POST":
        return JsonResponse(
            {
                "success": False,
                "error": "Only POST requests are allowed.",
            },
            status=405,
        )

    try:
        result = create_all_exam_registrations(session)

    except Exception as exc:
        return JsonResponse(
            {
                "success": False,
                "error": str(exc),
            },
            status=400,
        )

    return render(
        request,
        "exam_allocator/registration_result.html",
        {
            "session": session,
            "result": result,
        },
    )


def generate_allocation(request, exam_id):

    if request.method != "POST":
        return JsonResponse(
            {"error": "Only POST requests are allowed."},
            status=405,
        )

    try:
        result = run_allocation(exam_id)

    except AllocationWorkflowError as exc:
        return JsonResponse(
            {
                "success": False,
                "error": str(exc),
            },
            status=400,
        )

    return JsonResponse(
        {
            "success": True,
            "exam_id": result["exam"].exam_id,
            "subject_code": result["exam"].subject.subject_code,
            "subject_name": result["exam"].subject.subject_name,
            "students": len(result["students"]),
            "classrooms": len(result["classrooms"]),
            "groups": len(result["groups"]),
            "allocations": len(result["allocations"]),
        }
    )


def allocation_list(request, exam_id):

    exam = get_object_or_404(
        Exam.objects.select_related("subject"),
        exam_id=exam_id,
    )

    allocations = (
        Allocation.objects.filter(exam=exam)
        .select_related(
            "registration__student",
            "room",
        )
        .order_by(
            "room__room_number",
            "bench_number",
            "seat_number",
        )
    )

    return render(
        request,
        "exam_allocator/allocation_list.html",
        {
            "exam": exam,
            "allocations": allocations,
        },
    )


def upload_file(request, session_id):

    session = get_object_or_404(
        AllocationSession,
        session_id=session_id,
    )

    if request.method != "POST":
        return JsonResponse(
            {
                "success": False,
                "error": "Only POST requests are allowed.",
            },
            status=405,
        )

    uploaded = request.FILES.get("file")
    file_kind = request.POST.get("file_kind", "").strip().upper()

    if not uploaded:
        messages.error(request, "No file was uploaded.")

        return redirect(
            "exam_allocator:session_detail",
            session_id=session.session_id,
        )

    valid_kinds = {
        UploadedFile.FileKind.STUDENT_LIST,
        UploadedFile.FileKind.CLASSROOM_LIST,
        UploadedFile.FileKind.TIMETABLE,
    }

    if file_kind not in valid_kinds:
        messages.error(request, "Invalid file type.")

        return redirect(
            "exam_allocator:session_detail",
            session_id=session.session_id,
        )

    filename = uploaded.name.lower()

    if filename.endswith(".xlsx"):
        source_format = UploadedFile.SourceFormat.XLSX

    elif filename.endswith(".pdf"):
        source_format = UploadedFile.SourceFormat.PDF

    elif filename.endswith((".png", ".jpg", ".jpeg")):
        source_format = UploadedFile.SourceFormat.IMAGE

    else:
        messages.error(
            request,
            "Unsupported file format.",
        )

        return redirect(
            "exam_allocator:session_detail",
            session_id=session.session_id,
        )

    # ---------------------------------------------------------
    # Create UploadedFile record
    # ---------------------------------------------------------

    record = UploadedFile.objects.create(
        session=session,
        file=uploaded,
        original_filename=uploaded.name,
        file_kind=file_kind,
        source_format=source_format,
        status=UploadedFile.Status.PROCESSING,
    )

    try:

        # -----------------------------------------------------
        # Student List
        # -----------------------------------------------------

        if file_kind == UploadedFile.FileKind.STUDENT_LIST:

            if source_format != UploadedFile.SourceFormat.XLSX:
                raise ValueError(
                    "Student List currently supports Excel (.xlsx) files only."
                )

            result = parse_student_excel(record.file.path)

            stats = import_students(
                result,
                session,
            )

            message = (
                f"Student list imported successfully: "
                f"{stats['students_created']} students, "
                f"{stats['classes_created']} classes, "
                f"{stats['departments_created']} departments created."
            )

        # -----------------------------------------------------
        # Classroom List
        # -----------------------------------------------------

        elif file_kind == UploadedFile.FileKind.CLASSROOM_LIST:

            if source_format != UploadedFile.SourceFormat.XLSX:
                raise ValueError(
                    "Classroom List currently supports Excel (.xlsx) files only."
                )

            result = parse_classroom_excel(record.file.path)

            stats = import_classrooms(
                result,
                session,
            )

            message = (
                f"Classroom list imported successfully: "
                f"{stats['rooms_created']} rooms created."
            )

        # -----------------------------------------------------
        # Timetable
        # -----------------------------------------------------

        elif file_kind == UploadedFile.FileKind.TIMETABLE:

            if source_format != UploadedFile.SourceFormat.XLSX:
                raise ValueError(
                    "Exam Timetable currently supports Excel (.xlsx) files only."
                )

            result = parse_timetable_excel(record.file.path)

            stats = import_timetable(
                result,
                session,
            )

            message = (
                f"Timetable imported successfully: "
                f"{stats['subjects_created']} subjects, "
                f"{stats['exams_created']} exams, "
                f"{stats['targets_created']} targets created."
            )

        # -----------------------------------------------------
        # Mark upload as successfully processed
        # -----------------------------------------------------

        record.status = UploadedFile.Status.VALIDATED
        record.processed_at = timezone.now()
        record.error_log = ""
        record.save(
            update_fields=[
                "status",
                "processed_at",
                "error_log",
            ]
        )

        messages.success(
            request,
            message,
        )

    except Exception as exc:

        record.status = UploadedFile.Status.FAILED
        record.processed_at = timezone.now()
        record.error_log = str(exc)

        record.save(
            update_fields=[
                "status",
                "processed_at",
                "error_log",
            ]
        )

        messages.error(
            request,
            f"Import failed: {exc}",
        )

    return redirect(
        "exam_allocator:session_detail",
        session_id=session.session_id,
    )
