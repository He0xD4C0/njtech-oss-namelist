from django.db import migrations, models
from django.db.models.functions import Lower


def normalize_accounts(apps, schema_editor):
    from django.contrib.sessions.backends.db import SessionStore

    User = apps.get_model("core", "User")
    Address = apps.get_model("account", "EmailAddress")
    Confirmation = apps.get_model("account", "EmailConfirmation")
    alias = schema_editor.connection.alias
    users, addresses = User.objects.using(alias), Address.objects.using(alias)
    admins = list(users.filter(is_superuser=True).values_list("pk", "username"))
    collisions = list(addresses.filter(verified=True).annotate(normalized=Lower("email"))
                      .values("normalized").annotate(n=models.Count("user_id", distinct=True)).filter(n__gt=1))
    occupied = users.filter(username="admin").exclude(pk=admins[0][0] if len(admins) == 1 else None).exists()
    if len(admins) > 1 or occupied or collisions:
        raise RuntimeError(f"Account migration stopped: superusers={admins}, admin_occupied={occupied}, "
                           f"verified_email_conflicts={collisions}. Resolve ownership before retrying.")
    if admins:
        users.filter(pk=admins[0][0]).update(username="admin", is_active=True, is_staff=True)
    # An interrupted old bootstrap may point to an ordinary, unverified user.
    # Keep that account, but let the authorized wizard create the fixed admin.
    apps.get_model("core", "SiteState").objects.using(alias).filter(
        activated_at__isnull=True, bootstrap_user__is_superuser=False
    ).update(bootstrap_user=None)
    Confirmation.objects.using(alias).all().delete()
    Session = apps.get_model("sessions", "Session")
    decoder = SessionStore()
    for session in Session.objects.using(alias).iterator():
        data = decoder.decode(session.session_data)
        # Invalidate legacy in-flight signup/login state, which may contain an
        # unverified provider address. Preserve completed authenticated sessions.
        if "socialaccount_sociallogin" in data or "account_login" in data:
            Session.objects.using(alias).filter(pk=session.pk).delete()
    addresses.filter(verified=False).delete()
    for user in users.iterator():
        rows = list(addresses.filter(user_id=user.pk).order_by("pk"))
        # Preserve the current default, then the compatibility value, then oldest.
        preferred = next((r for r in rows if r.primary), None)
        preferred = preferred or next((r for r in rows if r.email.lower() == user.email.lower()), None)
        preferred = preferred or (rows[0] if rows else None)
        addresses.filter(user_id=user.pk).update(primary=False)
        kept = {}
        for row in rows:
            normalized = row.email.lower()
            if normalized in kept:
                addresses.filter(pk=row.pk).delete()
            else:
                kept[normalized] = row.pk
        for normalized, pk in kept.items():
            addresses.filter(pk=pk).update(email=normalized)
        email = preferred.email.lower() if preferred else ""
        if email:
            addresses.filter(pk=kept[email]).update(primary=True)
        users.filter(pk=user.pk).update(email=email)


def email_constraints(apps, schema_editor):
    # Database-only constraints on allauth's table; do not fork vendor migrations.
    if schema_editor.connection.vendor == "postgresql":
        # Finish deferred FK checks from the data repair before changing indexes,
        # retaining the single atomic transaction for the complete upgrade.
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')
    schema_editor.execute('CREATE UNIQUE INDEX account_bound_email_ci ON account_emailaddress (LOWER(email))')
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute('ALTER TABLE account_emailaddress ADD CONSTRAINT account_only_verified CHECK (verified)')
        schema_editor.execute("""CREATE FUNCTION core_fixed_username() RETURNS trigger AS $$
            BEGIN
              IF NEW.username <> OLD.username THEN RAISE EXCEPTION 'Username cannot be changed' USING ERRCODE = '23514'; END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql""")
        schema_editor.execute('CREATE TRIGGER core_fixed_username BEFORE UPDATE ON core_user FOR EACH ROW EXECUTE FUNCTION core_fixed_username()')
    else:
        for operation in ("INSERT", "UPDATE"):
            schema_editor.execute(f"""CREATE TRIGGER account_only_verified_{operation.lower()}
                BEFORE {operation} ON account_emailaddress WHEN NEW.verified = 0
                BEGIN SELECT RAISE(ABORT, 'Email must be verified before binding'); END""")
        schema_editor.execute("""CREATE TRIGGER core_fixed_username BEFORE UPDATE OF username ON core_user
            WHEN NEW.username <> OLD.username BEGIN SELECT RAISE(ABORT, 'Username cannot be changed'); END""")


def remove_email_constraints(apps, schema_editor):
    schema_editor.execute('DROP INDEX account_bound_email_ci')
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute('ALTER TABLE account_emailaddress DROP CONSTRAINT account_only_verified')
        schema_editor.execute('DROP TRIGGER core_fixed_username ON core_user')
        schema_editor.execute('DROP FUNCTION core_fixed_username()')
    else:
        for name in ("account_only_verified_insert", "account_only_verified_update", "core_fixed_username"):
            schema_editor.execute(f'DROP TRIGGER {name}')


class Migration(migrations.Migration):
    dependencies = [("core", "0005_backup_configuration"), ("account", "0009_emailaddress_unique_primary_email"),
                    ("sessions", "0001_initial")]
    operations = [
        migrations.RemoveConstraint(model_name="user", name="unique_normalized_user_email"),
        migrations.AlterField(model_name="user", name="email", field=models.EmailField(blank=True, max_length=254)),
        migrations.RunPython(normalize_accounts, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="user", constraint=models.CheckConstraint(
            condition=(models.Q(username="admin", is_superuser=True, is_staff=True, is_active=True)
                       | (~models.Q(username="admin") & models.Q(is_superuser=False))),
            name="fixed_admin_identity")),
        migrations.RunPython(email_constraints, remove_email_constraints),
    ]
