from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from accounts.access import is_workspace_admin

from .models import OutboundMailConfiguration
from .outbound import outbound_enabled
from .outbound_forms import OUTBOUND_FIELDS, OutboundMailForm


@sensitive_post_parameters("password")
@sensitive_variables()
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def outbound_settings(request):
    if not settings.HTTPS or not request.is_secure():
        raise Http404
    if not is_workspace_admin(request.user):
        raise PermissionDenied

    records = OutboundMailConfiguration.objects.filter(pk=1)
    if request.method == "POST":
        with transaction.atomic():
            record = records.select_for_update().first()
            form = OutboundMailForm(request.POST, configuration=record)
            if form.is_valid():
                fields = (*OUTBOUND_FIELDS, "encrypted_password", "linked_identity")
                OutboundMailConfiguration.objects.update_or_create(
                    pk=1, defaults={name: form.cleaned_data[name] for name in fields},
                )
                return redirect(reverse("outbound_settings", query={"gespeichert": "1"}))
    else:
        record = records.first()
        form = OutboundMailForm(configuration=record)

    return render(request, "mailimport/outbound_settings.html", {
        "section": "mailbox",
        "form": form,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
        "smtp_server_allowed": getattr(settings, "BMS_SMTP_ENABLED", False),
        "smtp_enabled": outbound_enabled(),
        "smtp_opted_in": bool(record and record.enabled),
    }, status=400 if form.is_bound else 200)
