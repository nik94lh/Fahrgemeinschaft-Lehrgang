from datetime import timedelta
from email.message import EmailMessage
from http import HTTPStatus

from django.core.files.uploadhandler import MemoryFileUploadHandler, StopUpload
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_safe

from accounts.access import can_view
from bookings.models import EmailEvent, School
from bookings.services import ingest

from .forms import OVERSIZED_FILE, EmlPreviewForm
from .parser import MAX_EML_BYTES, MailParseError, parse_eml
from .participant_settings import get_functional_addresses


CSP = (
    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


class _PreviewUploadHandler(MemoryFileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.error = ""
        self.error_status = HTTPStatus.BAD_REQUEST
        self.started = False

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        # Never fall back to a temporary file. Limit bytes as they arrive.
        self.activated = True

    def new_file(self, *args, **kwargs):
        if self.started:
            self.error = "Wähle genau eine EML-Datei aus."
            raise StopUpload(connection_reset=True)
        self.started = True
        super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > MAX_EML_BYTES:
            self.error = OVERSIZED_FILE
            self.error_status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            raise StopUpload(connection_reset=True)
        return super().receive_data_chunk(raw_data, start)


@csrf_exempt
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def preview(request):
    return _handle_upload(request, persist=False)


@csrf_exempt
@never_cache
@require_http_methods(["POST"])
def manual_import(request):
    return _handle_upload(request, persist=True)


def _handle_upload(request, *, persist):
    handler = _PreviewUploadHandler(request)
    if request.method == "POST":
        # Install the bounded handler before CSRF validation reads the multipart body.
        request.upload_handlers = [handler]
    response = _render_preview(request, handler, persist=persist)
    response.headers["Content-Security-Policy"] = CSP
    return response


@csrf_protect
def _render_preview(request, handler, *, persist):
    result = None
    status = HTTPStatus.OK
    form = EmlPreviewForm()
    if request.method == "POST":
        form = EmlPreviewForm(request.POST, request.FILES, upload_error=handler.error)
        if not form.is_valid():
            status = handler.error_status
        else:
            file = form.cleaned_data["file"]
            try:
                raw = file.read(MAX_EML_BYTES + 1)
                if persist:
                    event = ingest(raw)
                    if event.state == EmailEvent.State.NEEDS_REVIEW:
                        if can_view(request.user, "review"):
                            return redirect("review_detail", event_id=event.pk)
                        return redirect(f"{reverse('preview')}?klaerung=1")
                    return redirect(f"{reverse('bookings')}?importiert=1")
                result = parse_eml(raw, functional_addresses=get_functional_addresses())
            except MailParseError as error:
                form.add_error("file", str(error))
                status = HTTPStatus.BAD_REQUEST

    context = {
        "section": "preview",
        "form": form,
        "result": result,
        "review_created": request.GET.get("klaerung") == "1",
        "event_label": EmailEvent.Kind(result.event_type).label if result else None,
        "school_label": School(result.school).label if result and result.school else None,
    }
    return render(request, "mailimport/preview.html", context, status=status)


@never_cache
@require_safe
def sample_eml(request):
    start = timezone.localdate() + timedelta(days=7)
    end = start + timedelta(days=2)
    message = EmailMessage()
    message["Subject"] = "Lehrgangsanmeldung wurde bestätigt"
    message.set_content(
        "Teilnehmer: Alex Beispiel\n"
        "Lehrgangsart: Synthetischer Testlehrgang\n"
        "Schulungsort: SFS Würzburg\n"
        f"Lehrgangstermin: {start:%d.%m.%Y} 09:00 Uhr – {end:%d.%m.%Y} 15:00 Uhr\n"
        f"Buchungsreferenz: DEMO-{start:%Y%m%d}\n"
    )
    return HttpResponse(
        message.as_bytes(),
        content_type="message/rfc822",
        headers={"Content-Disposition": 'attachment; filename="bms-testmail.eml"'},
    )
