from uuid import uuid4

from .models import AccountSessionRevision


SESSION_REVISION_KEY = "bms_session_revision"


def _revision(user):
    revision = AccountSessionRevision.objects.filter(user=user).values_list("revision", flat=True).first()
    return str(revision) if revision is not None else None


def revoke_account_sessions(user):
    AccountSessionRevision.objects.update_or_create(user=user, defaults={"revision": uuid4()})


def remember_session_revision(request, user):
    request.session[SESSION_REVISION_KEY] = _revision(user)


def session_revision_is_current(request, user):
    # Existing sessions have no revision and remain valid until their account is revoked.
    return request.session.get(SESSION_REVISION_KEY) == _revision(user)
