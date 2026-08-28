from django.urls import path

from . import views

app_name = "exam_allocator"


urlpatterns = [
    path(
        "",
        views.exam_list,
        name="exam_list",
    ),
    path(
        "exam/<int:exam_id>/generate/",
        views.generate_allocation,
        name="generate_allocation",
    ),
    path(
        "exam/<int:exam_id>/allocations/",
        views.allocation_list,
        name="allocation_list",
    ),
]
