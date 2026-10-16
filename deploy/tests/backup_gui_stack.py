"""Opt-in acceptance against ONLY the disposable njtech-backup-gui stack."""
import json
import os
import subprocess
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "test-results/backup-gui"
COMPOSE = ["docker", "compose", "--profile", "ops", "-p", "njtech-backup-gui", "-f", "compose.yaml", "-f", "deploy/compose.local.yaml", "-f", "var/backup-gui/compose.json"]


def command(*args, timeout=180):
    result = subprocess.run([*COMPOSE, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        print(result.stderr, flush=True)
        print(result.stdout, flush=True)
        result.check_returncode()
    return result.stdout


def shell(code, service="web"):
    return command("exec", "-T", service, "python", "manage.py", "shell", "-c", code).strip().splitlines()[-1]


def wait_run(run_id):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        state = json.loads(shell(f"import json; from core.models import SyncRun; r=SyncRun.objects.get(pk={run_id}); print(json.dumps([r.status,r.message]))"))
        if state[0] not in {"queued", "running"}:
            assert state[0] == "success", state
            return state
        time.sleep(2)
    raise AssertionError("Backup operation timed out")


def main():
    if os.environ.get("NAMELIST_BACKUP_ACCEPTANCE") != "1":
        raise SystemExit("Requires explicit NAMELIST_BACKUP_ACCEPTANCE=1 and the disposable test stack")
    assert shell("from django.conf import settings; print(settings.SITE_BASE_URL)") == "http://localhost:18004"
    assert shell("from django.conf import settings; print(settings.DEBUG)") == "False"
    assert shell("from core.models import SiteState; print(SiteState.objects.filter(activated_at__isnull=False).exists())") == "False"
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    shell("""import boto3
s=boto3.client('s3',endpoint_url='https://backup-s3',aws_access_key_id='test-backup-access',aws_secret_access_key='test-backup-secret-only',region_name='us-east-1')
if 'backups' not in [b['Name'] for b in s.list_buckets()['Buckets']]: s.create_bucket(Bucket='backups')
print('bucket-ready')""", "maintenance")
    shell("""from core.models import SiteState,User,PolicyDocument
from allauth.account.models import EmailAddress
from core.runtime import set_setting
u,_=User.objects.get_or_create(username='backup-fixture',defaults={'email':'backup@example.invalid'})
u.set_password('test-only-not-for-deployment'); u.save()
EmailAddress.objects.update_or_create(user=u,email=u.email,defaults={'verified':True,'primary':True})
SiteState.objects.update_or_create(pk=1,defaults={'bootstrap_user':u,'activated_at':None})
for k,v in {'smtp_host':'test.invalid','smtp_password':'test-only','smtp_verified':'1','turnstile_site_key':'test-only','turnstile_secret_key':'test-only'}.items(): set_setting(k,v)
for kind in ['privacy','terms','inclusion']: PolicyDocument.objects.update_or_create(kind=kind,language='zh-CN',defaults={'body':'fixture'})
print('seeded')""")
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto("http://localhost:18004/setup/")
        page.get_by_label("服务器初始化密钥").fill("local-development-bootstrap")
        page.get_by_role("button", name="验证并开始").click()
        expect(page.get_by_role("heading", name="确认管理员邮箱", exact=True)).to_be_visible()
        page.goto("http://localhost:18004/setup/?step=operations&task=branding")
        from PIL import Image
        picture = BytesIO()
        Image.new("RGBA", (128, 128), (50, 90, 130, 128)).save(picture, "PNG")
        page.get_by_label("选择新的 Logo", exact=True).set_input_files({"name":"logo.png", "mimeType":"image/png", "buffer":picture.getvalue()})
        page.get_by_role("button", name="上传并应用 Logo", exact=True).click()
        expect(page.get_by_alt_text("当前站点 Logo", exact=True)).to_be_visible()
        expect(page.locator('[data-brand-logo]')).to_be_visible()
        page.screenshot(path=str(ARTIFACTS / "container-logo-1440.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":1000})
        page.screenshot(path=str(ARTIFACTS / "container-logo-390.png"),full_page=True)
        page.set_viewport_size({"width":1440,"height":1000})
        page.goto("http://localhost:18004/setup/?step=operations&task=backup")
        for key,value in {'endpoint':'https://backup-s3','bucket':'backups','region':'us-east-1','access_key':'test-backup-access','secret_key':'test-backup-secret-only'}.items():
            page.locator(f'[name="{key}"]').fill(value)
        page.get_by_role("button", name="保存并测试连接",exact=True).click()
        expect(page.get_by_role("button",name="启用自动备份",exact=True)).to_be_enabled(timeout=45000)
        page.get_by_role("button",name="启用自动备份",exact=True).click()
        expect(page.get_by_text("已有成功的数据库备份。",exact=True)).to_be_visible(timeout=180000)
        page.screenshot(path=str(ARTIFACTS / "container-backup-1440.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":1000})
        page.screenshot(path=str(ARTIFACTS / "container-backup-390.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("button",name="立即执行全量备份",exact=True).click()
        time.sleep(1)
        run_id=int(shell("from core.models import SyncRun; print(SyncRun.objects.filter(kind='backup-backup').latest('pk').pk)"))
        wait_run(run_id)
        browser.close()
    command("exec","-T","maintenance","/app/deploy/backup/backup.sh","diff",timeout=240)
    info=json.loads(command("exec","-T","maintenance","python","-c","from deploy.backup.managed import command; print(command('info',extra=('--output=json',)).stdout)"))
    assert {'full','diff'} <= {b['type'] for s in info for b in s.get('backup',[])}
    bad_run = int(shell("from core.backup import save_candidate,queue_operation; save_candidate({'secret_key':'deliberately-invalid-fixture'}); print(queue_operation('test').pk)"))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        outcome = shell(f"from core.models import SyncRun; print(SyncRun.objects.get(pk={bad_run}).status)")
        if outcome == "failed":
            break
        time.sleep(1)
    assert outcome == "failed"
    assert shell("from deploy.backup import managed; print(managed.active()['values']['secret_key']=='test-backup-secret-only')", "maintenance") == "True"
    command("stop","web","redis",timeout=120)
    command("exec","-T","maintenance","/app/deploy/backup/backup.sh","full",timeout=240)
    # Force a due tick in the real independent scheduler, without waiting until 03:15.
    command("exec","-T","maintenance","python","-c","from deploy.backup import scheduler; scheduler.due=lambda *_: True; scheduler.stopping.wait=lambda _: scheduler.stopping.set() or True; scheduler.main()",timeout=240)
    restored=command("exec","-T","-e","RESTORE_RESERVE_BYTES=1048576","maintenance","/app/deploy/backup/restore-drill.sh",timeout=360)
    (ARTIFACTS / "isolated-restore.log").write_text(restored)
    # An enabled-but-lost config must retain WAL. Restore only this disposable fixture file afterwards.
    command("exec","-T","maintenance","python","-c","from deploy.backup import managed; p=managed.ROOT/'active.json'; p.rename(p.with_suffix('.saved'))")
    archive=subprocess.run([*COMPOSE,"exec","-T","db","python3","/app/deploy/backup/pgbackrest_archive.py","pg_wal/test-missing-config"],cwd=ROOT,capture_output=True,text=True)
    assert archive.returncode != 0
    command("exec","-T","maintenance","python","-c","from deploy.backup import managed; p=managed.ROOT/'active.json'; p.with_suffix('.saved').rename(p)")
    command("start","redis","web")
    (ARTIFACTS / "container-result.json").write_text(json.dumps({'gui_test_apply_manual':'passed','full_and_diff':'passed','without_web_and_redis':'backup_and_restore_passed','missing_config':'archive_failed_closed'},indent=2))
    print('GUI, TLS S3, full/differential/WAL backups, independent restore and fail-closed acceptance passed.',flush=True)


if __name__ == '__main__':
    main()
