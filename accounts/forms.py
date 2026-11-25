from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Permission

from .access import SECTIONS, section_permission


User = get_user_model()
ACCESS_CHOICES = (
    ("none", "Kein Zugriff"),
    ("view", "Ansehen"),
    ("edit", "Bearbeiten"),
)


def _workspace_permissions():
    codenames = [
        section_permission(section, action).split(".", 1)[1]
        for section in SECTIONS for action in ("view", "edit")
    ]
    return {
        f"accounts.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames)
    }


class WorkspaceRightsMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        direct_rights = set()
        if self.instance.pk:
            direct_rights = set(self.instance.user_permissions.filter(
                content_type__app_label="accounts",
            ).values_list("codename", flat=True))
        for section, label in SECTIONS.items():
            initial = next((
                action for action in ("edit", "view")
                if section_permission(section, action).split(".", 1)[1] in direct_rights
            ), "none")
            self.fields[f"access_{section}"] = forms.ChoiceField(
                label=label, choices=ACCESS_CHOICES, initial=initial,
            )

    def access_fields(self):
        return (self[f"access_{section}"] for section in SECTIONS)

    def save_rights(self, user):
        permissions = _workspace_permissions()
        selected = set()
        for section in SECTIONS:
            choice = self.cleaned_data[f"access_{section}"]
            if choice in ("view", "edit"):
                selected.add(permissions[section_permission(section, "view")])
            if choice == "edit":
                selected.add(permissions[section_permission(section, "edit")])
        user.user_permissions.remove(*(set(permissions.values()) - selected))
        user.user_permissions.add(*selected)


class AccountCreateForm(WorkspaceRightsMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)


class AccountEditForm(WorkspaceRightsMixin, forms.ModelForm):
    is_active = forms.BooleanField(label="Konto aktiv", required=False)

    class Meta:
        model = User
        fields = ("is_active",)
