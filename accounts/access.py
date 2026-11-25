from django.conf import settings
from django.urls import reverse


SECTIONS = {
    "overview": "Übersicht",
    "bookings": "Buchungen",
    "people": "Personen",
    "calendar": "Kalender",
    "rides": "Fahrgemeinschaften",
    "brigades": "Feuerwehren",
    "review": "Klärung",
    "preview": "E-Mail-Vorschau",
    "mailbox": "Postfach",
    "mail_receipts": "Mail-Eingang",
}
HTTPS_SECTIONS = frozenset({"mailbox", "mail_receipts"})
READ_ONLY_SECTIONS = frozenset({"people"})
PRIMARY_NAVIGATION = ("overview", "bookings", "people", "calendar", "rides", "review")
SECONDARY_NAVIGATION = ("brigades", "mail_receipts", "preview", "mailbox")


def section_actions(section):
    return ("view",) if section in READ_ONLY_SECTIONS else ("view", "edit")


def section_permission(section, action):
    if section not in SECTIONS or action not in section_actions(section):
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
    return "edit" in section_actions(section) and _has_section_permission(user, section, "edit")


def can_view(user, section):
    return _has_section_permission(user, section, "view") or can_edit(user, section)


def home_url(user):
    if not is_active_user(user):
        return reverse("login")
    for section in SECTIONS:
        if (settings.HTTPS or section not in HTTPS_SECTIONS) and can_view(user, section):
            return reverse(section)
    return reverse("account")


def _navigation_items(sections, view_permissions, is_secure):
    return [
        {"section": section, "label": SECTIONS[section], "href": reverse(section)}
        for section in sections
        if view_permissions[section] and (is_secure or section not in HTTPS_SECTIONS)
    ]


def workspace_permissions(request):
    user = request.user
    view_permissions = {section: can_view(user, section) for section in SECTIONS}
    admin = is_workspace_admin(user)
    secure = request.is_secure()
    primary = _navigation_items(PRIMARY_NAVIGATION, view_permissions, secure)
    secondary = _navigation_items(SECONDARY_NAVIGATION, view_permissions, secure)
    if admin:
        secondary.append({"section": "accounts", "label": "Benutzer", "href": reverse("account_list")})
    return {
        "workspace_view": view_permissions,
        "workspace_edit": {section: can_edit(user, section) for section in SECTIONS},
        "workspace_admin": admin,
        "workspace_home": home_url(user),
        "workspace_primary_navigation": primary,
        "workspace_secondary_navigation": secondary,
    }
