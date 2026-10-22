"""Exercise the actual upgrade, including preflight rollback on legacy conflicts."""
import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)
OLD = [('core', '0005_backup_configuration')]
NEW = [('core', '0006_account_identity')]


@pytest.fixture
def legacy():
    executor = MigrationExecutor(connection)
    executor.migrate(OLD)
    apps = executor.loader.project_state(OLD + [('account', '0009_emailaddress_unique_primary_email'),
                                              ('sessions', '0001_initial')]).apps
    yield apps
    # A failed preflight deliberately leaves legacy records untouched. Remove
    # only this test's fixtures before restoring the current schema.
    apps.get_model('core', 'User').objects.all().delete()
    current = MigrationExecutor(connection)
    current.migrate(current.loader.graph.leaf_nodes())


def test_upgrade_preserves_ids_passwords_and_verified_defaults(legacy):
    User = legacy.get_model('core', 'User')
    Address = legacy.get_model('account', 'EmailAddress')
    Session = legacy.get_model('sessions', 'Session')
    root = User.objects.create(username='old-root', email='root@example.test', password='hash', is_superuser=True)
    user = User.objects.create(username='unchanged', email='second@example.test', password='same-hash')
    Address.objects.create(user_id=user.pk, email='FIRST@example.test', verified=True, primary=True)
    Address.objects.create(user_id=user.pk, email='first@example.test', verified=True)
    Address.objects.create(user_id=user.pk, email='second@example.test', verified=True)
    Address.objects.create(user_id=root.pk, email='root@example.test', verified=False, primary=True)
    Session.objects.create(session_key='pending', session_data=SessionStore().encode({
        'socialaccount_sociallogin': {'email': 'pending@example.test'}}), expire_date=timezone.now())
    Session.objects.create(session_key='signed-in', session_data=SessionStore().encode({
        '_auth_user_id': str(user.pk)}), expire_date=timezone.now())
    MigrationExecutor(connection).migrate(NEW)
    root.refresh_from_db()
    user.refresh_from_db()
    assert root.username == 'admin' and root.email == '' and root.password == 'hash'
    assert root.is_superuser and root.is_staff and root.is_active
    assert user.username == 'unchanged' and user.password == 'same-hash' and user.email == 'first@example.test'
    assert list(Address.objects.filter(user_id=user.pk, primary=True).values_list('email', flat=True)) == ['first@example.test']
    assert Address.objects.count() == 2 and not Address.objects.filter(verified=False).exists()
    assert list(Session.objects.values_list('pk', flat=True)) == ['signed-in']


@pytest.mark.parametrize('conflict', ['roots', 'reserved', 'email'])
def test_upgrade_conflicts_stop_without_mutating_accounts(legacy, conflict):
    User = legacy.get_model('core', 'User')
    Address = legacy.get_model('account', 'EmailAddress')
    one = User.objects.create(username='old-root', email='one@example.test', is_superuser=True)
    two = User.objects.create(username='admin' if conflict == 'reserved' else 'other',
                              email='two@example.test', is_superuser=conflict == 'roots')
    Address.objects.create(user_id=one.pk, email='pending@example.test', verified=False)
    if conflict == 'email':
        Address.objects.create(user_id=one.pk, email='same@example.test', verified=True)
        Address.objects.create(user_id=two.pk, email='SAME@example.test', verified=True)
    with pytest.raises(RuntimeError, match='Account migration stopped'):
        MigrationExecutor(connection).migrate(NEW)
    one.refresh_from_db()
    assert one.username == 'old-root' and one.email == 'one@example.test'
    assert Address.objects.filter(verified=False).exists()
