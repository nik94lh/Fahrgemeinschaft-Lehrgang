from http import HTTPStatus

from django.core.files.uploadhandler import MemoryFileUploadHandler, StopUpload
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from bookings.models import EmailEvent, School

from .forms import OVERSIZED_FILE, EmlPreviewForm
from .parser import MAX_EML_BYTES, MailParseError, parse_eml


CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
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
    handler = _PreviewUploadHandler(request)
    if request.method == "POST":
        # Install the bounded handler before CSRF validation reads the multipart body.
        request.upload_handlers = [handler]
    response = _render_preview(request, handler)
    response.headers["Content-Security-Policy"] = CSP
    return response


@csrf_protect
def _render_preview(request, handler):
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
                result = parse_eml(file.read(MAX_EML_BYTES + 1))
            except MailParseError as error:
                form.add_error("file", str(error))
                status = HTTPStatus.BAD_REQUEST

    context = {
        "form": form,
        "result": result,
        "event_label": EmailEvent.Kind(result.event_type).label if result else None,
        "school_label": School(result.school).label if result and result.school else None,
    }
    return render(request, "mailimport/preview.html", context, status=status)
