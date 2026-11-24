from django import forms

from .parser import MAX_EML_BYTES


OVERSIZED_FILE = "Die EML-Datei darf höchstens 1 MiB groß sein."


class EmlPreviewForm(forms.Form):
    file = forms.FileField(
        label="EML-Datei",
        required=False,
        widget=forms.FileInput(attrs={
            "accept": ".eml,message/rfc822",
            "aria-describedby": "eml-hint",
            "required": True,
        }),
        error_messages={"empty": "Die EML-Datei ist leer."},
    )

    def __init__(self, *args, upload_error="", **kwargs):
        super().__init__(*args, **kwargs)
        self.upload_error = upload_error

    def clean_file(self):
        if self.upload_error:
            raise forms.ValidationError(self.upload_error)

        file = self.cleaned_data["file"]
        if file is None:
            raise forms.ValidationError("Wähle eine EML-Datei aus.")
        if not file.name.lower().endswith(".eml"):
            raise forms.ValidationError("Wähle eine Datei mit der Endung .eml aus.")
        if file.size > MAX_EML_BYTES:
            raise forms.ValidationError(OVERSIZED_FILE)
        return file
