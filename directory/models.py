from django.db import models


class RankingMonth(models.Model):
    """An immutable month seal, including an empty month's result."""

    month = models.DateField(unique=True)
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-month"]


class RankingEntry(models.Model):
    month = models.ForeignKey(RankingMonth, on_delete=models.CASCADE, related_name="entries")
    submission = models.ForeignKey("repositories.Submission", null=True, on_delete=models.SET_NULL)
    platform = models.CharField(max_length=12)
    platform_id = models.CharField(max_length=100)
    position = models.PositiveSmallIntegerField()
    gain = models.IntegerField()
    ending_stars = models.IntegerField()
    first_observed_at = models.DateTimeField()
    last_observed_at = models.DateTimeField()
    metadata = models.JSONField(default=dict)

    class Meta:
        ordering = ["platform", "position"]
        constraints = [
            models.UniqueConstraint(fields=["month", "platform", "position"], name="unique_monthly_rank"),
            models.UniqueConstraint(fields=["month", "submission"], name="one_rank_per_month_claim"),
        ]
