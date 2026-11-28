"""Participant-host views for ride inquiries."""

from functools import wraps
from pathlib import Path

from django import forms
from django.http import FileResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.utils.cache import add_never_cache_headers
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from .models import RideNotice, RideParticipation
from .ride_participation import (
    CONTACT_CHANNELS,
    active_participation,
    participation_for_code,
    portal_overview,
    record_ride_reply,
    validate_ride_phone,
)


PORTAL_GRANT = "ride_participation_grant"
CODE_ERROR = "Dieser Zugangscode ist ungültig oder abgelaufen."
SESSION_ERROR = "Dieser Zugang ist nicht mehr gültig. Gib deinen Zugangscode erneut ein."
PHONE_CHANNELS = {"whatsapp", "call"}


class AccessCodeForm(forms.Form):
    code = forms.CharField(
        label="Zugangscode", max_length=43,
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "none", "spellcheck": "false"}),
    )


class RideReplyForm(forms.Form):
    answer = forms.ChoiceField(
        label="Hast du Interesse an der Fahrgemeinschaft?", choices=(("yes", "Ja"), ("no", "Nein")),
        widget=forms.RadioSelect,
    )
    brigade = forms.CharField(label="Feuerwehr (optional)", max_length=120, required=False)
    channels = forms.MultipleChoiceField(
        label="Über welche Kanäle dürfen dich die anderen erreichen?", choices=CONTACT_CHANNELS,
        widget=forms.CheckboxSelectMultiple, required=False,
    )
    phone = forms.CharField(
        label="Telefonnummer", max_length=25, required=False, help_text="Mit Vorwahl angeben.",
        widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"}),
    )
    consent = forms.BooleanField(
        label="Ich willige ein, dass BMS meinen Namen, meine angegebene Feuerwehr und die gewählten Kontaktdaten mit den zustimmenden Personen dieser Fahrgemeinschaft teilt.",
        required=False,
    )

    def __init__(self, *args, is_test=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_test = is_test
        if is_test:
            self.fields["phone"].label = "Testtelefon"
            self.fields["phone"].help_text = "Zum Beispiel 000 000 001."

    def clean(self):
        values = super().clean()
        if values.get("answer") != "yes":
            return values
        channels = values.get("channels", ())
        if "channels" in values and not channels:
            self.add_error("channels", "Wähle mindestens einen Kontaktkanal.")
        if "phone" in values:
            if PHONE_CHANNELS.intersection(channels):
                try:
                    validate_ride_phone(values["phone"], is_test=self.is_test)
                except ValueError as error:
                    self.add_error("phone", str(error))
            else:
                values["phone"] = ""
        if not values.get("consent"):
            self.add_error("consent", "Stimme der Weitergabe zu, um mit Ja zu antworten.")
        return values


def _private_response(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        add_never_cache_headers(response)
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.headers.get("Content-Type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    return wrapped


def _grant(request):
    grant = request.session.get(PORTAL_GRANT)
    return (grant["participation_id"], grant["token_digest"]) if grant else (None, None)


def _latest_contact_note(participation):
    if participation.answer != RideParticipation.Answer.YES or not participation.consented_at:
        return ""
    # A later release or consent supersedes an older withdrawal.
    kind = participation.notices.filter(
        created_at__gte=participation.consented_at,
        kind__in=(RideNotice.Kind.CONTACTS, RideNotice.Kind.NEW_PEER, RideNotice.Kind.WITHDRAWAL),
    ).order_by("-created_at", "-pk").values_list("kind", flat=True).first()
    if kind == RideNotice.Kind.WITHDRAWAL:
        return "Eine Zusage wurde nachträglich zurückgezogen."
    return ""


@sensitive_variables("participation", "form", "context")
def _render_portal(request, *, participation=None, form=None, access_error="", status=200):
    context = {"code_form": AccessCodeForm(), "access_error": access_error}
    if participation is not None:
        if form is None:
            form = RideReplyForm(is_test=participation.case.is_test, initial={
                "answer": participation.answer if participation.answer != "pending" else "",
                "brigade": participation.brigade,
                "channels": participation.channels,
                "phone": participation.phone,
                "consent": bool(participation.consented_at),
            })
        possible_peer_count, contacts = portal_overview(participation)
        context.update(
            participation=participation,
            reply_form=form,
            possible_peer_count=possible_peer_count,
            contacts=contacts,
            contact_note=_latest_contact_note(participation),
        )
    return render(request, "ride_portal/page.html", context, status=status)


@sensitive_post_parameters("code")
@sensitive_variables("form", "participation", "code")
@_private_response
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_portal(request):
    if request.method == "POST":
        if request.POST.get("action") == "logout":
            request.session.flush()
            rotate_token(request)
            return redirect("ride_portal")
        request.session.pop(PORTAL_GRANT, None)
        form = AccessCodeForm(request.POST)
        if request.POST.get("action", "code") == "code" and form.is_valid():
            code = form.cleaned_data["code"]
            participation = participation_for_code(code)
            if participation is not None:
                request.session.flush()
                request.session[PORTAL_GRANT] = {
                    "participation_id": participation.pk, "token_digest": participation.token_digest,
                }
                rotate_token(request)
                return redirect("ride_portal")
        return _render_portal(request, access_error=CODE_ERROR, status=400)

    grant = _grant(request)
    participation = active_participation(*grant) if grant[0] is not None else None
    if grant[0] is not None and participation is None:
        request.session.pop(PORTAL_GRANT, None)
        return _render_portal(request, access_error=SESSION_ERROR)
    return _render_portal(request, participation=participation)


@sensitive_post_parameters("phone", "brigade")
@sensitive_variables("participation", "form", "grant", "values")
@_private_response
@require_POST
@csrf_protect
def ride_portal_reply(request):
    grant = _grant(request)
    participation = active_participation(*grant) if grant[0] is not None else None
    if participation is None:
        request.session.pop(PORTAL_GRANT, None)
        return _render_portal(request, access_error=SESSION_ERROR, status=403)

    form = RideReplyForm(request.POST, is_test=participation.case.is_test)
    if form.is_valid():
        try:
            record_ride_reply(*grant, **form.cleaned_data)
        except ValueError as error:
            participation = active_participation(*grant)
            if participation is None:
                request.session.pop(PORTAL_GRANT, None)
                return _render_portal(request, access_error=SESSION_ERROR, status=403)
            form.add_error(None, str(error))
        else:
            return redirect("ride_portal")
    return _render_portal(request, participation=participation, form=form, status=400)


@_private_response
@require_safe
def ride_portal_script(request):
    script = Path(__file__).with_name("ride_portal.js")
    return FileResponse(script.open("rb"), content_type="text/javascript; charset=utf-8")
