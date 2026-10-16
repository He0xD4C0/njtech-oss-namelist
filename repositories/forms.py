from django import forms

from core.i18n import text
from providers.api import parse_repository_url


class RepositoryURLForm(forms.Form):
    repository_url = forms.CharField(max_length=600, widget=forms.TextInput(attrs={
        "placeholder": "https://github.com/username/repository",
        "inputmode": "url", "autocapitalize": "none", "spellcheck": "false",
    }))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["repository_url"]
        field.label = text("仓库地址", "Repository URL")
        field.help_text = text(
            "支持 GitHub、Gitee 和 GitLab.com 的仓库主页或 HTTPS 克隆地址（可带 .git）。",
            "Paste a GitHub, Gitee or GitLab.com repository URL or HTTPS clone URL (with or without .git).",
        )
        field.error_messages["required"] = text("请先粘贴仓库地址。", "Paste a repository URL first.")

    def clean_repository_url(self):
        value = self.cleaned_data["repository_url"]
        try:
            parse_repository_url(value)
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None
        return value
