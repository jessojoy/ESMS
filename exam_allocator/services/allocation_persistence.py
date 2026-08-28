from django.db import transaction

from exam_allocator.models import (
    Allocation,
    Exam,
    ExamRegistration,
    Room,
)

STREAM_TO_SEAT_NUMBER = {
    "A": 1,
    "B": 2,
    "C": 3,
}


@transaction.atomic
def save_seat_plan(exam: Exam, seat_plan):
    """
    Save an engine SeatPlan into Django Allocation records.

    Existing allocations for this exam are deleted first, allowing
    the allocation to be safely regenerated.
    """

    rooms = {room.room_number: room for room in Room.objects.all()}

    registrations = {
        registration.student.roll_number: registration
        for registration in (
            ExamRegistration.objects.filter(exam=exam).select_related("student")
        )
    }

    # Validate engine output before changing the database.
    for seat in seat_plan.seats:

        if seat.room_no not in rooms:
            raise ValueError(f"Engine generated unknown room: {seat.room_no}")

        if seat.student.roll_no not in registrations:
            raise ValueError(
                f"Student {seat.student.roll_no} is not "
                f"registered for exam {exam.subject.subject_code}"
            )

        if seat.stream not in STREAM_TO_SEAT_NUMBER:
            raise ValueError(f"Unknown engine stream: {seat.stream}")

    # Replace any previous allocation for this exam.
    Allocation.objects.filter(exam=exam).delete()

    allocations = []

    for seat in seat_plan.seats:

        room = rooms[seat.room_no]

        registration = registrations[seat.student.roll_no]

        allocations.append(
            Allocation(
                registration=registration,
                exam=exam,
                room=room,
                bench_number=seat.bench_no,
                seat_number=STREAM_TO_SEAT_NUMBER[seat.stream],
            )
        )

    Allocation.objects.bulk_create(allocations)

    return allocations
