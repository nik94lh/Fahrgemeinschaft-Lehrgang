"""Account-scoped ride mediation exercise using only fixed fictional contacts."""

from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe

from django import forms
from django.contrib.auth import get_user_model
from django.core.validators import RegexValidator
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_safe

from .models import RideSimulationInvitation


RESPONDENTS = {
    1: ("Alex Musterfahrt", "alex.musterfahrt@example.invalid"),
    2: ("Sam Übungsfahrt", "sam.uebungsfahrt@example.invalid"),
    3: ("Toni Testfahrt", "toni.testfahrt@example.invalid"),
}
BRIGADES = (
    ("", "Keine Angabe"),
    ("muster", "Testfeuerwehr Musterort"),
    ("uebung", "Testfeuerwehr Übungsort"),
)
ANSWER_CHOICES = (("", "Bitte auswählen"), ("yes", "Ja"), ("no", "Nein"))
SESSION_GRANTS = "ride_simulation_grants"


class SimulationAnswerForm(forms.Form):
    interest = forms.ChoiceField(
        label="Hast du Interesse an der Fahrgemeinschaft?", choices=ANSWER_CHOICES,
    )
    driver = forms.ChoiceField(
        label="Kannst du fahren?", choices=ANSWER_CHOICES, required=False,
    )
    brigade = forms.ChoiceField(label="Testfeuerwehr", choices=BRIGADES, required=False)
    phone = forms.CharField(
        label="Testtelefon", required=False, max_length=25, help_text="Verwende eine fiktive Nummer wie 000 000 001.",
        widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"}),
        validators=[RegexValidator(
            r"\A000(?:[ -]?[0-9]){3,12}\Z", "Verwende eine Testnummer mit der Vorwahl 000.",
        )],
    )

    def clean(self):
        values = super().clean()
        if values.get("interest") == "yes" and not values.get("driver"):
            self.add_error("driver", "Gib an, ob du fahren kannst.")
        return values


def _digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


@sensitive_variables("token", "previews")
@transaction.atomic
def _issue_test_links(user, *, renew):
    get_user_model().objects.select_for_update().get(pk=user.pk)
    owned = RideSimulationInvitation.objects.filter(owner=user)
    if renew:
        invitations = list(owned.select_for_update().filter(answered_at__isnull=True).order_by("respondent"))
    else:
        owned.delete()
        invitations = [RideSimulationInvitation(owner=user, respondent=respondent) for respondent in RESPONDENTS]
    expires_at = timezone.now() + timedelta(hours=2)
    previews = []
    open_url = reverse("ride_simulation_open")
    for invitation in invitations:
        token = token_urlsafe(32)
        invitation.token_digest = _digest(token)
        invitation.expires_at = expires_at
        name, email = RESPONDENTS[invitation.respondent]
        previews.append({"name": name, "email": email, "link": f"{open_url}#{token}"})
    if renew:
        owned.bulk_update(invitations, ["token_digest", "expires_at"])
    else:
        owned.bulk_create(invitations)
    return previews


@sensitive_variables("previews")
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_simulation(request):
    previews = []
    if request.method == "POST":
        action = request.POST.get("action", "start")
        if action not in {"start", "renew"}:
            return HttpResponseBadRequest("Unbekannte Simulationsaktion.")
        previews = _issue_test_links(request.user, renew=action == "renew")
        if previews:
            request.session.pop(SESSION_GRANTS, None)
    now = timezone.now()
    counts = RideSimulationInvitation.objects.filter(owner=request.user).aggregate(
        yes=Count("pk", filter=Q(interest=True)),
        no=Count("pk", filter=Q(interest=False)),
        open=Count("pk", filter=Q(interest__isnull=True, expires_at__gt=now)),
        expired=Count("pk", filter=Q(interest__isnull=True, expires_at__lte=now)),
    )
    return render(request, "workspace/ride_simulation.html", {
        "section": "rides",
        "counts": counts if any(counts.values()) else None,
        "previews": previews,
        "can_renew": not previews and bool(counts["open"] or counts["expired"]),
    })


def _available(user):
    return RideSimulationInvitation.objects.filter(owner=user, answered_at__isnull=True, expires_at__gt=timezone.now())


def _unavailable(request):
    return render(request, "workspace/ride_simulation_open.html", {
        "section": "rides", "unavailable": True,
    }, status=404)


@sensitive_post_parameters("token")
@sensitive_variables("token")
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_simulation_open(request):
    if request.method != "POST":
        return render(request, "workspace/ride_simulation_open.html", {"section": "rides"})
    token = request.POST.get("token", "")
    if len(token) != 43:
        return _unavailable(request)
    invitation = _available(request.user).filter(token_digest=_digest(token)).first()
    if invitation is None:
        return _unavailable(request)
    grants = request.session.get(SESSION_GRANTS, {}).copy()
    grants[str(invitation.pk)] = invitation.token_digest
    request.session[SESSION_GRANTS] = grants
    return redirect("ride_simulation_response", invitation_id=invitation.pk)


@sensitive_post_parameters("phone")
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_simulation_response(request, invitation_id):
    digest = request.session.get(SESSION_GRANTS, {}).get(str(invitation_id))
    invitations = _available(request.user).filter(pk=invitation_id, token_digest=digest)
    if request.method == "POST":
        with transaction.atomic():
            invitation = invitations.select_for_update().first()
            now = timezone.now()
            if invitation is None or invitation.expires_at <= now:
                return _unavailable(request)
            form = SimulationAnswerForm(request.POST)
            if form.is_valid():
                values = form.cleaned_data
                invitation.interest = values["interest"] == "yes"
                invitation.driver = (values["driver"] == "yes") if invitation.interest else None
                invitation.brigade = values["brigade"] if invitation.interest else ""
                invitation.phone = values["phone"] if invitation.interest else ""
                invitation.answered_at = now
                invitation.save(update_fields=["interest", "driver", "brigade", "phone", "answered_at"])
                return redirect("ride_simulation")
    else:
        invitation = invitations.first()
        if invitation is None:
            return _unavailable(request)
        form = SimulationAnswerForm()

    name, email = RESPONDENTS[invitation.respondent]
    return render(request, "workspace/ride_simulation_response.html", {
        "section": "rides", "form": form, "name": name, "email": email,
    }, status=400 if form.is_bound else 200)


@never_cache
@require_safe
def ride_simulation_script(request):
    return FileResponse(Path(__file__).with_name("ride_simulation.js").open("rb"), content_type="text/javascript; charset=utf-8")
