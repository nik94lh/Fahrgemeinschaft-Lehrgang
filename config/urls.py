from django.http import JsonResponse
from django.urls import path

from mailimport.views import preview


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", preview, name="preview"),
    path("health/", health, name="health"),
]
