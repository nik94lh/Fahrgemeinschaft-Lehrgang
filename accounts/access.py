from django.conf import settings
from django.urls import reverse


SECTIONS = {
    "overview": "Übersicht",
    "bookings": "Buchungen",
    "calendar": "Kalender",
    "rides": "Fahrgemeinschaften",
    "brigades": "Feuerwehren",
    "review": "Klärung",
    "preview": "E-Mail-Vorschau",
    "mailbox": "Postfach",
    "mail_receipts": "Mail-Eingang",
}
HTTPS_SECTIONS = frozenset({"mailbox", "mail_receipts"})


def section_permission(section, action):
    if section not in SECTIONS or action not in {"view", "edit"}:
        raise ValueError(f"Unbekanntes BMS-Recht: {action} {section}")
    return f"accounts.{action}_{section}"


def is_active_user(user):
    return user.is_authenticated and user.is_active


def is_workspace_admin(user):
    return is_active_user(user) and user.is_superuser


def _has_section_permission(user, section, action):
    # The account administration controls direct workspace grants.
    return is_active_user(user) and (
        user.is_superuser or section_permission(section, action) in user.get_user_permissions()
    )


def can_edit(user, section):
    return _has_section_permission(user, section, "edit")


def can_view(user, section):
    return _has_section_permission(user, section, "view") or can_edit(user, section)


def home_url(user):
    if not is_active_user(user):
        return reverse("login")
    for section in SECTIONS:
        if (settings.HTTPS or section not in HTTPS_SECTIONS) and can_view(user, section):
            return reverse(section)
    return reverse("account")


def workspace_permissions(request):
    user = request.user
    return {
        "workspace_view": {section: can_view(user, section) for section in SECTIONS},
        "workspace_edit": {section: can_edit(user, section) for section in SECTIONS},
        "workspace_admin": is_workspace_admin(user),
        "workspace_home": home_url(user),
    }
