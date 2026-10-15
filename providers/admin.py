from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib import admin

# OAuth settings belong to the encrypted runtime panel. Credentials and
# identity deletion must not have an alternate plaintext/raw-model admin path.
for model in (SocialApp, SocialToken, SocialAccount):
    if admin.site.is_registered(model):
        admin.site.unregister(model)
