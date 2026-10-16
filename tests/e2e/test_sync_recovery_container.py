"""Opt-in process-loss acceptance; touches only the fixed local test project."""
import json
import os
import subprocess
import time

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.getenv("NAMELIST_RECOVERY_CONTAINER") != "1", reason="Isolated fault stack is opt-in")]
PREFIX = "njtech-sync-recovery"
ORIGIN = "http://localhost:18003"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=30)


def shell(code):
    output = docker("exec", PREFIX + "-web-1", "python", "manage.py", "shell", "-c",
                    "import json\n" + code)
    return json.loads(next(line.removeprefix("RESULT:") for line in output.splitlines() if line.startswith("RESULT:")))


def inspect_runs(ids):
    return shell(f'''
from pathlib import Path
from django.conf import settings
from core.models import SyncRun
from core.sync import recovery_state
root = Path(settings.WORK_ROOT) / 'recovery-fixtures'
rows=[]
for r in SyncRun.objects.filter(pk__in={ids!r}).order_by('pk'):
    marker=root / f'{{r.pk}}.started'
    attempts=root / f'{{r.pk}}.attempts'
    rows.append(dict(id=r.pk, kind=r.kind, status=r.status, recovery=recovery_state(r),
                     expires=r.lease_expires_at.timestamp() if r.lease_expires_at else None,
                     pid=int(marker.read_text()) if marker.exists() else None,
                     attempts=len(attempts.read_text().splitlines()) if attempts.exists() else 0))
print('RESULT:'+json.dumps(rows))
''')


def wait_runs(ids, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = inspect_runs(ids)
        if predicate(rows):
            return rows
        time.sleep(1)
    raise AssertionError(inspect_runs(ids))


def enqueue_pair():
    return shell('''
from core.sync import queue_sync
print('RESULT:'+json.dumps([queue_sync(kind)[0].pk for kind in ('documents', 'licenses')]))
''')


def test_real_worker_loss_auto_and_manual_recovery(browser, artifact_dir):
    for service in ("web", "worker", "db", "redis"):
        label = docker("inspect", PREFIX + f"-{service}-1", "--format", '{{index .Config.Labels "com.docker.compose.project"}}').strip()
        assert label == PREFIX
    assert shell('''
from django.conf import settings
from core.models import User
print('RESULT:'+json.dumps({'debug':settings.DEBUG, 'origin':settings.SITE_BASE_URL, 'users':User.objects.count()}))
''') == {"debug": False, "origin": ORIGIN, "users": 0}
    shell('''
from allauth.account.models import EmailAddress
from core.models import User, SiteState, PolicyDocument
from core.runtime import set_setting
u=User.objects.create_superuser('admin', email='fixture@example.invalid')
EmailAddress.objects.create(user=u, email=u.email, primary=True, verified=True)
SiteState.objects.update_or_create(pk=1, defaults={'bootstrap_user':u})
for key,value in {'smtp_host':'mailpit', 'smtp_verified':'1', 'turnstile_site_key':'fixture', 'turnstile_secret_key':'fixture',
                  'policy_source_url':'https://github.com/example/documents.git', 'license_source_url':'https://github.com/example/licenses.git'}.items():
    set_setting(key,value)
for kind in ('privacy','terms','inclusion'):
    PolicyDocument.objects.create(kind=kind, language='zh-CN', body='Fixture policy', source_sha='initial')
print('RESULT:true')
''')
    ids = enqueue_pair()
    first = wait_runs(ids, lambda rows: len(rows) == 2 and all(r["pid"] for r in rows))
    # Observe actual 30-second renewal before terminating both execution processes.
    renewed = wait_runs(ids, lambda rows: all(r["expires"] > f["expires"] + 20 for r, f in zip(rows, first)), timeout=50)
    pids = [r["pid"] for r in renewed]
    docker("exec", PREFIX + "-worker-1", "python", "-c",
           f"import os,signal; pids={pids!r}; assert all(p>1 for p in pids); [os.kill(p,signal.SIGKILL) for p in pids]")
    automatic = wait_runs(ids, lambda rows: all(r["status"] in {"success", "unchanged"} for r in rows), timeout=180)
    assert all(r["attempts"] == 2 for r in automatic)
    # Whole-worker outage leaves the original messages unacknowledged in Redis.
    manual_ids = enqueue_pair()
    wait_runs(manual_ids, lambda rows: len(rows) == 2 and all(r["pid"] for r in rows))
    docker("stop", "--time", "1", PREFIX + "-worker-1")
    wait_runs(manual_ids, lambda rows: all(r["recovery"] == "recoverable" for r in rows), timeout=150)
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.goto(ORIGIN + "/setup/")
    page.get_by_label("服务器初始化密钥").fill("local-development-bootstrap")
    page.get_by_role("button", name="验证并开始").click()
    expect(page.get_by_role("heading", name="确认管理员邮箱", exact=True)).to_be_visible()
    for kind in ("documents", "licenses"):
        page.goto(ORIGIN + f"/setup/?step=requirements&task={kind}")
        expect(page.get_by_role("button", name="重新尝试", exact=True)).to_be_visible()
        page.screenshot(path=str(artifact_dir / f"container-recovery-{kind}.png"), full_page=True)
        page.get_by_role("button", name="重新尝试", exact=True).click()
        if kind == "documents":
            expect(page.get_by_text("请求已提交，正在等待执行", exact=False)).to_be_visible()
            docker("start", PREFIX + "-worker-1")
        expect(page.get_by_text("本次同步已完成" if kind == "documents" else "检查完成，已是最新内容", exact=False)).to_be_visible(timeout=20000)
    manual = inspect_runs(manual_ids)
    assert all(r["attempts"] == 2 for r in manual)
    # Late original deliveries cannot fetch or publish the same completed request again.
    shell(f'''
from core.models import SyncRun, SourceSyncState
from core.runtime import get_setting
from core.tasks import sync_documents, sync_license_policy
for r in SyncRun.objects.filter(pk__in={manual_ids!r}):
    task=sync_documents if r.kind=='documents' else sync_license_policy
    source=get_setting('policy_source_url' if r.kind=='documents' else 'license_source_url')
    task.delay(run_id=r.pk, source=source, generation=SourceSyncState.objects.get(kind=r.kind).generation)
print('RESULT:true')
''')
    time.sleep(3)
    assert all(r["attempts"] == 2 for r in inspect_runs(manual_ids))
    (artifact_dir / "container-recovery-result.json").write_text(json.dumps({
        "automatic": automatic, "manual": manual, "heartbeat": "actual 30-second renewal",
        "lease": "actual 120-second expiry", "database": "PostgreSQL", "broker": "Redis",
        "worker_loss": "SIGKILL execution processes and whole-worker stop",
        "external_fixtures": "Git fetch and ScanCode compile only; no real external service claim",
    }, indent=2))
    context.close()
