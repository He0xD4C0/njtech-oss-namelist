from django.utils.dateparse import parse_datetime


def repository_card(submission, metadata=None):
    repo = submission.repository
    historical = submission.status == "historical"
    data = metadata if metadata is not None else (submission.public_metadata if historical else {})
    fields = (
        "name",
        "full_name",
        "description",
        "html_url",
        "fork",
        "archived",
        "upstream_url",
        "upstream_name",
        "stars",
        "commit_at",
    )
    defaults = {"fork": False, "archived": False, "stars": 0, "commit_at": None}
    # Missing fields in a historical snapshot must not leak later private/transfer metadata.
    preserved = historical or metadata is not None
    card = {
        field: data.get(field, defaults.get(field, "")) if preserved else getattr(repo, field)
        for field in fields
    }
    if isinstance(card["commit_at"], str):
        card["commit_at"] = parse_datetime(card["commit_at"])
    card.update(
        submission=submission,
        user=submission.user,
        platform=repo.platform,
        platform_label=repo.get_platform_display(),
        license_labels=data.get("license_labels", submission.license_labels),
        historical=historical,
    )
    return card
