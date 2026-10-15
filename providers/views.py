from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from core.runtime import provider_enabled

from .forms import ConnectionsDisconnectForm, DisconnectForm
from .models import PLATFORMS, OAuthBinding


@login_required
def index(request):
    bindings = {binding.platform: binding for binding in OAuthBinding.objects.filter(user=request.user)}
    platforms = [{"id": platform, "name": name, "enabled": provider_enabled(platform),
                  "binding": bindings.get(platform), "login_url": reverse(f"{platform}_login")}
                 for platform, name in PLATFORMS]
    return render(request, "providers/index.html", {"platforms": platforms})


@login_required
@require_POST
def disconnect(request):
    form = DisconnectForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("请确认解绑平台及其收录内容 / Please confirm disconnection and withdrawal")
    from core.services import disconnect_platform
    disconnect_platform(get_user_model().objects.get(pk=request.user.pk), form.cleaned_data["platform"])
    messages.success(request, "平台已解绑，相关公开内容已撤下。 / Platform disconnected; its public entries have been withdrawn.")
    return redirect("providers:index")


@login_required
@require_http_methods(["GET", "POST"])
def connections_compat(request):
    """Mount before allauth at /accounts/3rdparty/; disabled apps can still be disconnected."""
    if request.method == "GET":
        return redirect("providers:index")
    form = ConnectionsDisconnectForm(request.POST, user=request.user)
    if not form.is_valid():
        return HttpResponseBadRequest("请选择本人绑定的平台账号 / Select one of your connected platform accounts")
    from core.services import disconnect_platform
    disconnect_platform(get_user_model().objects.get(pk=request.user.pk), form.cleaned_data["account"].provider)
    messages.success(request, "平台已解绑，相关公开内容已撤下。 / Platform disconnected; its public entries have been withdrawn.")
    return redirect("providers:index")

