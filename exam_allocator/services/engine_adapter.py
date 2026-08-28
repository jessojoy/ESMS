from exam_allocator.models import ExamRegistration, Room

from engine.models.student import Student as EngineStudent
from engine.models.classroom import Classroom as EngineClassroom


def get_section(class_name):
    parts = class_name.strip().split()

    if not parts:
        return ""

    last = parts[-1].upper()

    # B.Arch 2K22 A
    if last in {"A", "B", "C", "D", "E", "F", "T"}:
        return last

    # CE 2K23A / CS 2K23A / etc.
    if last[-1].isalpha():
        return last[-1]

    return ""


def django_registration_to_engine_student(registration):
    student = registration.student
    exam = registration.exam

    student_class = student.student_class
    department = student_class.department

    return EngineStudent(
        register_no=student.roll_number,
        name=student.student_name,
        department=department.department_code,
        semester=student_class.semester,
        section=get_section(student_class.class_name),
        subject_code=exam.subject.subject_code,
        subject_name=exam.subject.subject_name,
        exam_date=exam.exam_date.strftime("%d-%m-%Y"),
        session=exam.session,
        roll_no=student.roll_number,
    )


def get_engine_students(exam):
    registrations = (
        ExamRegistration.objects.filter(exam=exam)
        .select_related(
            "student",
            "student__student_class",
            "student__student_class__department",
            "exam",
            "exam__subject",
        )
        .order_by(
            "student__student_class__class_name",
            "student__roll_number",
        )
    )

    return [
        django_registration_to_engine_student(registration)
        for registration in registrations
    ]


def django_room_to_engine_classroom(room):
    if room.capacity != 45:
        raise ValueError(
            f"Room {room.room_number} has capacity " f"{room.capacity}; expected 45."
        )

    if room.benches != 15:
        raise ValueError(
            f"Room {room.room_number} has " f"{room.benches} benches; expected 15."
        )

    return EngineClassroom(
        room_no=room.room_number,
        rows=5,
        benches_per_row=3,
        seats_per_bench=3,
    )


def get_engine_classrooms():
    rooms = Room.objects.all().order_by("room_number")

    return [django_room_to_engine_classroom(room) for room in rooms]
