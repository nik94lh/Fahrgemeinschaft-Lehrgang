from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers, patch_vary_headers

from .access import can_edit, can_view, is_active_user, is_workspace_admin
from .sessions import session_revision_is_current


PUBLIC_ROUTES = frozenset({
    "health", "live_script", "workspace_chrome_script", "calendar_timeline_script", "favicon", "login",
})
ACCOUNT_ROUTES = frozenset({"account", "account_password_change", "logout"})
ADMIN_ROUTES = frozenset({"account_list", "account_create", "account_edit", "account_password_reset"})
VIEW_ROUTES = {
    "overview": "overview",
    "bookings": "bookings",
    "booking_detail": "bookings",
    "booking_participant": "bookings",
    "people": "people",
    "calendar": "calendar",
    "calendar_detail": "calendar",
    "rides": "rides",
    "ride_detail": "rides",
    "brigades": "brigades",
    "review": "review",
    "review_detail": "review",
    "preview": "preview",
    "sample_eml": "preview",
    "mailbox": "mailbox",
    "mail_receipts": "mail_receipts",
    "mail_receipt": "mail_receipts",
}
EDIT_ROUTES = {
    "rides_sync": "rides",
    "rides_settings": "rides",
    "ride_simulation": "rides",
    "ride_simulation_open": "rides",
    "ride_simulation_response": "rides",
    "ride_simulation_script": "rides",
    "brigade_create": "brigades",
    "brigade_edit": "brigades",
    "manual_import": "preview",
    "mail_receipt_recheck": "mail_receipts",
}
EDIT_ON_POST = frozenset({"booking_participant", "ride_detail", "review_detail", "mailbox"})
BOOKING_EDIT_ROUTES = frozenset({"manual_import", "review_detail", "mail_receipt_recheck"})


def _allows_workspace_route(request, route):
    user = request.user
    if route in ACCOUNT_ROUTES:
        return True
    if route in ADMIN_ROUTES:
        return is_workspace_admin(user)

    edit_section = EDIT_ROUTES.get(route)
    if route in EDIT_ON_POST and request.method == "POST":
        edit_section = VIEW_ROUTES[route]
    if edit_section is not None:
        if not can_edit(user, edit_section):
            return False
        if route in BOOKING_EDIT_ROUTES or (
            route == "mailbox" and request.POST.get("action") != "save-functional-addresses"
        ):
            return can_edit(user, "bookings")
        return True

    view_section = VIEW_ROUTES.get(route)
    return view_section is not None and can_view(user, view_section)


class WorkspaceAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        route = request.resolver_match.view_name
        if route in PUBLIC_ROUTES and route != "login":
            return None
        if is_active_user(request.user) and not session_revision_is_current(request, request.user):
            logout(request)
        if route in PUBLIC_ROUTES:
            return None

        live_request = request.headers.get("X-BMS-Live") == "1"
        if not is_active_user(request.user):
            if live_request:
                response = HttpResponse(status=401)
            else:
                response = redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        elif not _allows_workspace_route(request, route):
            if live_request:
                response = HttpResponseForbidden("Für diese Seite fehlt die Berechtigung.", content_type="text/plain")
            else:
                response = render(request, "accounts/forbidden.html", status=403)
        else:
            return None

        add_never_cache_headers(response)
        patch_vary_headers(response, ("X-BMS-Live",))
        return response
