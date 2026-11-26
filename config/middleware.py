"""Security policy for the server-rendered BMS pages."""

from django.conf import settings
from django.http.request import split_domain_port
from django.utils.cache import add_never_cache_headers, patch_vary_headers


def is_ride_portal_host(request):
    domain, _ = split_domain_port(request.META.get("HTTP_HOST", ""))
    return bool(settings.RIDE_PORTAL_HOST and domain == settings.RIDE_PORTAL_HOST)


class ContentPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if is_ride_portal_host(request):
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
            add_never_cache_headers(response)
        if response.get("Content-Type", "").startswith("text/html"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            patch_vary_headers(response, ("X-BMS-Live",))
            if request.headers.get("X-BMS-Live") == "1":
                add_never_cache_headers(response)
        return response
