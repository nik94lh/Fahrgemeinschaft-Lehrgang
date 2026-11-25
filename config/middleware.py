"""Security policy for the server-rendered BMS pages."""

from django.utils.cache import add_never_cache_headers, patch_vary_headers


class ContentPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            patch_vary_headers(response, ("X-BMS-Live",))
            if request.headers.get("X-BMS-Live") == "1":
                add_never_cache_headers(response)
        return response
