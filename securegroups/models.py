import time
from collections import defaultdict

from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import (
    EveAllianceInfo, EveCorporationInfo, EveFactionInfo,
)
from allianceauth.services.hooks import get_extension_logger

from . import app_settings, filter as smart_filters

if app_settings.discord_bot_active():
    import aadiscordbot

logger = get_extension_logger(__name__)


def _format_exemptions_html(alliances, corporations) -> str:
    """Human-readable HTML fragment describing exempt alliances/corporations, or "" if none."""
    alliance_names = [a.alliance_name for a in alliances]
    corp_names = [c.corporation_name for c in corporations]
    if not alliance_names and not corp_names:
        return ""
    bits = []
    if alliance_names:
        bits.append(format_html("alliance(s) {}", ", ".join(alliance_names)))
    if corp_names:
        bits.append(format_html("corporation(s) {}", ", ".join(corp_names)))
    return format_html(
        " (exempt regardless of the above if main character is in {})",
        format_html_join(" or ", "{}", ((b,) for b in bits))
    )


EXPLAIN_INDENT = "1em"


def _auto_explain(instance) -> str:
    """
    Generic, introspection-based explanation for a filter object that has no
    `explain()` of its own. Many apps (e.g. allianceauth-corp-tools) define
    their own filter models that duck-type securegroups' FilterBase rather
    than subclassing it, so this must work off the model's own fields alone -
    it renders every configured (non-empty) field as "label: value" so admins
    get something useful regardless of which app defines the filter.
    """
    skip = {"id", "name", "description"}
    lines = []
    for field in instance._meta.get_fields():
        if not getattr(field, "concrete", False) or field.name in skip:
            continue
        try:
            if field.many_to_many:
                values = list(getattr(instance, field.name).all())
                if not values:
                    continue
                value_str = ", ".join(str(v) for v in values)
            elif field.is_relation:
                value = getattr(instance, field.name)
                if value is None:
                    continue
                value_str = str(value)
            else:
                value = getattr(instance, field.name)
                if value in (None, ""):
                    continue
                value_str = str(value)
        except Exception:
            continue
        label = getattr(field, "verbose_name", field.name)
        lines.append(format_html("<div>{}: {}</div>", label, value_str))

    verbose_name = instance._meta.verbose_name
    if not lines:
        return format_html('<span style="opacity:0.7;">{}</span>', verbose_name)
    return format_html(
        '<span style="opacity:0.7;">{}</span>'
        '<div style="padding-left:{};">{}</div>',
        verbose_name,
        EXPLAIN_INDENT,
        format_html_join("", "{}", ((l,) for l in lines))
    )


class GroupUpdateWebhook(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    webhook = models.TextField()
    extra_message = models.TextField(default="", blank=True)

    def __str__(self):
        return "Group Update Hook for: %s" % self.group.name


class SmartFilter(models.Model):
    class Meta:
        verbose_name = "Smart Filter Binding"
        verbose_name_plural = "Smart Filter Catalog"

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, editable=False
    )
    object_id = models.PositiveIntegerField(editable=False)
    filter_object = GenericForeignKey("content_type", "object_id")
    grace_period = models.IntegerField(default=5)

    def __str__(self):
        try:
            return f"{self.filter_object.name}: {self.filter_object.description}"
        except:  # noqa: E722
            return f"Error: {self.content_type.app_label}:{self.content_type} {self.object_id} Not Found"

    def _admin_edit_url(self):
        """Admin change-page URL for the concrete filter object, or None if unavailable."""
        try:
            return reverse(
                f"admin:{self.content_type.app_label}_{self.content_type.model}_change",
                args=[self.object_id]
            )
        except NoReverseMatch:
            return None

    def explain(self) -> str:
        """
        HTML-safe, human-readable explanation of the bound filter's actual logic,
        regardless of its concrete type. Guards against broken/dangling generic
        relations so a single misconfigured filter can't break the whole tree.
        """
        try:
            filter_obj = self.filter_object
        except Exception as e:
            return format_html('<span class="text-danger">Error loading filter: {}</span>', e)
        if filter_obj is None:
            return format_html(
                '<span class="text-danger">Broken filter reference ({} id={})</span>',
                self.content_type, self.object_id
            )
        try:
            if hasattr(filter_obj, "explain"):
                body = filter_obj.explain()
            else:
                body = _auto_explain(filter_obj)
        except Exception as e:
            return format_html('<span class="text-danger">Error explaining {}: {}</span>', filter_obj, e)

        title = format_html(
            "{}: {}", getattr(filter_obj, "name", ""), getattr(filter_obj, "description", "")
        )
        edit_url = self._admin_edit_url()
        if edit_url:
            header = format_html(
                '<strong>{}</strong> <a href="{}" target="_blank">[edit]</a>', title, edit_url
            )
        else:
            header = format_html("<strong>{}</strong>", title)

        return format_html(
            '<div>{}<div style="padding-left:{};">{}</div></div>',
            header, EXPLAIN_INDENT, body
        )


class FilterBase(models.Model):

    name = models.CharField(max_length=500)
    description = models.CharField(max_length=500)

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.name}: {self.description}"

    def process_filter(self, user: User):
        raise NotImplementedError("Please Create a filter!")

    def audit_filter(self, users):
        raise NotImplementedError("Please Create an audit function!")

    def explain(self) -> str:
        """
        HTML-safe, human-readable explanation of this filter's actual configured
        logic (as opposed to its free-text `description`). Defaults to a generic
        field dump; override on concrete filters for nicer wording.
        """
        return _auto_explain(self)


class FilterExpression(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Expression"
        verbose_name_plural = verbose_name

    first_term = models.ForeignKey(
        SmartFilter,
        on_delete=models.CASCADE,
        related_name="+"
    )

    class OperatorChoices(models.TextChoices):
        AND = "and"
        OR = "or"
        XOR = "xor"

    operator = models.CharField(
        max_length=10,
        choices=OperatorChoices.choices,
    )

    second_term = models.ForeignKey(
        SmartFilter,
        on_delete=models.CASCADE,
        related_name="+",
    )

    negate_result = models.BooleanField(default=False)

    def explain(self) -> str:
        first_html = self.first_term.explain()
        second_html = self.second_term.explain()
        box = format_html(
            '<div style="border-left:2px solid #888; padding-left:{};">'
            '{}<div style="opacity:0.7;">{}</div>{}'
            '</div>',
            EXPLAIN_INDENT, first_html, self.operator.upper(), second_html
        )
        if self.negate_result:
            return format_html(
                '<div><span style="opacity:0.7;">NOT of the following:</span>{}</div>',
                box
            )
        return box

    def process_filter(self, user: User):
        first = self.first_term.filter_object.process_filter(user)
        second = self.second_term.filter_object.process_filter(user)

        result = False

        if self.operator == self.OperatorChoices.AND:
            result = first and second
        elif self.operator == self.OperatorChoices.OR:
            result = first or second
        elif self.operator == self.OperatorChoices.XOR:
            result = first ^ second
        else:
            return False

        if self.negate_result:
            result = not result
        return result

    def audit_filter(self, users):
        first_res = self.first_term.filter_object.audit_filter(users)
        second_res = self.second_term.filter_object.audit_filter(users)

        output = defaultdict(lambda: {"message": "", "check": False})

        for user in users:
            first = first_res[user.id]
            second = second_res[user.id]

            if self.operator == self.OperatorChoices.AND:
                check = first["check"] and second["check"]
                if self.negate_result:
                    check = not check

                output[user.id] = {
                    "check": check,
                    "message": f"{'NOT ' if self.negate_result else ''}{first['message']} AND {second['message']}",
                }
            elif self.operator == self.OperatorChoices.OR:
                check = first["check"] or second["check"]
                if self.negate_result:
                    check = not check

                output[user.id] = {
                    "check": check,
                    "message": f"{'NOT ' if self.negate_result else ''}{first['message']} OR {second['message']}",
                }
            elif self.operator == self.OperatorChoices.XOR:
                check = first["check"] ^ second["check"]
                if self.negate_result:
                    check = not check

                output[user.id] = {
                    "check": check,
                    "message": f"{'NOT ' if self.negate_result else ''}{first['message']} XOR {second['message']}",
                }
            else:
                output[user.id]["check"] = False
                output[user.id]["message"] = "Invalid operator"

        return output


class AltCorpFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Character in Corporation"
        verbose_name_plural = verbose_name

    alt_corp = models.ForeignKey(EveCorporationInfo, on_delete=models.CASCADE)

    # sometimes there are double standards.
    exempt_alliances = models.ManyToManyField(
        EveAllianceInfo, related_name="corp_exempt_alliances", blank=True)
    exempt_corporations = models.ManyToManyField(
        EveCorporationInfo, related_name="corp_exempt_corporations", blank=True)

    def explain(self) -> str:
        try:
            corp = format_html("{} [{}]", self.alt_corp.corporation_name, self.alt_corp.corporation_id)
        except Exception:
            corp = "an unknown/deleted corporation"
        exempt = _format_exemptions_html(self.exempt_alliances.all(), self.exempt_corporations.all())
        return format_html("User (or an alt) is in corporation <strong>{}</strong>{}", corp, exempt)

    def process_filter(self, user: User):
        return smart_filters.check_alt_corp_on_account(
            user, self.alt_corp.corporation_id,
            exempt_allis=self.exempt_alliances.all().values_list("alliance_id", flat=True),
            exempt_corps=self.exempt_corporations.all().values_list("corporation_id", flat=True)
        )

    def audit_filter(self, users):
        co = CharacterOwnership.objects.filter(user__in=users, character__corporation_id=self.alt_corp.corporation_id).values(
            'user__id', 'character__character_name')

        chars = defaultdict(list)
        for c in co:
            chars[c['user__id']].append(c['character__character_name'])

        output = defaultdict(lambda: {"message": "", "check": False})
        for c, char_list in chars.items():
            output[c] = {"message": ", ".join(char_list), "check": True}
        return output


class AltAllianceFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Character in Alliance"
        verbose_name_plural = verbose_name

    alt_alli = models.ForeignKey(EveAllianceInfo, on_delete=models.CASCADE)

    # sometimes there are double standards.
    exempt_alliances = models.ManyToManyField(
        EveAllianceInfo, related_name="alli_exempt_alliances", blank=True)
    exempt_corporations = models.ManyToManyField(
        EveCorporationInfo, related_name="alli_exempt_corporations", blank=True)

    def explain(self) -> str:
        try:
            alli = format_html("{} [{}]", self.alt_alli.alliance_name, self.alt_alli.alliance_id)
        except Exception:
            alli = "an unknown/deleted alliance"
        exempt = _format_exemptions_html(self.exempt_alliances.all(), self.exempt_corporations.all())
        return format_html("User (or an alt) is in alliance <strong>{}</strong>{}", alli, exempt)

    def process_filter(self, user: User):
        return smart_filters.check_alt_alli_on_account(user, self.alt_alli.alliance_id,
                                                       exempt_allis=self.exempt_alliances.all().values_list("alliance_id", flat=True),
                                                       exempt_corps=self.exempt_corporations.all().values_list("corporation_id", flat=True)
                                                       )

    def audit_filter(self, users):
        co = CharacterOwnership.objects.filter(user__in=users, character__alliance_id=self.alt_alli.alliance_id).values(
            'user__id', 'character__character_name')
        chars = defaultdict(list)
        for c in co:
            chars[c['user__id']].append(c['character__character_name'])

        output = defaultdict(lambda: {"message": "", "check": False})
        for c, char_list in chars.items():
            output[c] = {"message": ", ".join(char_list), "check": True}
        return output


class UserInGroupFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: User Has Group"
        verbose_name_plural = verbose_name

    groups = models.ManyToManyField(Group, related_name="in_group")

    # sometimes there are double standards.
    exempt_alliances = models.ManyToManyField(
        EveAllianceInfo, related_name="group_exempt_alliances", blank=True)
    exempt_corporations = models.ManyToManyField(
        EveCorporationInfo, related_name="group_exempt_corporations", blank=True)

    reversed_logic = models.BooleanField(default=False)

    def explain(self) -> str:
        group_names = sorted(g.name for g in self.groups.all())
        groups_html = ", ".join(group_names) if group_names else "(no groups configured)"
        verb = "does NOT belong" if self.reversed_logic else "belongs"
        exempt = _format_exemptions_html(self.exempt_alliances.all(), self.exempt_corporations.all())
        return format_html("User {} to any of auth group(s) <strong>{}</strong>{}", verb, groups_html, exempt)

    def process_filter(self, user: User):
        result = smart_filters.check_group_on_account(
            user,
            self.groups.all(),
            exempt_allis=self.exempt_alliances.all().values_list("alliance_id", flat=True),
            exempt_corps=self.exempt_corporations.all().values_list("corporation_id", flat=True)
        )
        if self.reversed_logic:
            return not result
        return result

    def audit_filter(self, users):
        cl = users.prefetch_related('groups').filter(
            groups__id__in=self.groups.all().values_list("id", flat=True))
        chars = defaultdict(
            lambda: {"message": "", "check": self.reversed_logic})
        for c in cl:
            chars[c.id] = {"message": "", "check": not self.reversed_logic}
        return chars


class AltFactionFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Character in Faction"
        verbose_name_plural = verbose_name

    alt_faction = models.ForeignKey(EveFactionInfo, on_delete=models.CASCADE)

    def explain(self) -> str:
        try:
            fac = format_html("{} [{}]", self.alt_faction.faction_name, self.alt_faction.faction_id)
        except Exception:
            fac = "an unknown/deleted faction"
        return format_html("User (or an alt) is in faction <strong>{}</strong>", fac)

    def process_filter(self, user: User):
        return smart_filters.check_alt_fac_on_account(
            user,
            self.alt_faction.faction_id,
        )

    def audit_filter(self, users):
        co = CharacterOwnership.objects.filter(
            user__in=users,
            character__faction_id=self.alt_faction.faction_id
        ).values(
            'user__id',
            'character__character_name'
        )
        chars = defaultdict(list)
        for c in co:
            chars[c['user__id']].append(c['character__character_name'])

        output = defaultdict(lambda: {"message": "", "check": False})
        for c, char_list in chars.items():
            output[c] = {"message": ", ".join(char_list), "check": True}
        return output


class SmartGroup(models.Model):
    group = models.OneToOneField(Group, on_delete=models.CASCADE)
    description = models.CharField(max_length=500, default="", blank=True)
    filters = models.ManyToManyField(SmartFilter)
    last_update = models.DateTimeField(auto_now=True)
    last_run = models.DateTimeField(null=True, blank=True, editable=False)
    last_scheduled = models.DateTimeField(null=True, blank=True, editable=False)
    last_run_timing = models.TextField(blank=True, default="", editable=False)

    auto_group = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    include_in_updates = models.BooleanField(default=True)

    can_grace = models.BooleanField(default=False)

    notify_on_remove = models.BooleanField(default=True)
    notify_on_grace = models.BooleanField(default=True)
    notify_on_add = models.BooleanField(default=False)

    class Meta:
        permissions = (
            ("access_sec_group", "Can access sec group requests screen."),
            ("audit_sec_group", "Can audit sec groups members."),)

    def __str__(self):
        return "Smart Group: %s" % self.group.name

    def explain(self) -> str:
        """Full HTML explanation of every requirement this group enforces, regardless of filter type."""
        items = format_html_join(
            "", '<div style="margin-top:0.5em;">{}</div>',
            ((sf.explain(),) for sf in self.filters.all())
        )
        if not items:
            return format_html("<em>No filters configured.</em>")
        return format_html(
            "<p>User must satisfy <strong>ALL</strong> of the following:</p>{}",
            items
        )

    def run_checks(self, user: User):
        output = []
        group_start = time.perf_counter()
        for check in self.filters.all():
            try:
                _filter = check.filter_object
                if _filter is None:
                    logger.warning(f"Failed to run filter for {check}")
                    continue  # Skip as this is broken...
                t0 = time.perf_counter()
                test_pass = _filter.process_filter(user)
                logger.debug(
                    "run_checks [%s] filter '%s' took %.3fs",
                    self.group.name, check, time.perf_counter() - t0
                )
            except:  # noqa: E722
                test_pass = False
                logger.error("TEST FAILED")  # TODO Make pretty
            _check = {
                "name": check.filter_object.description,
            }
            _check["check"] = test_pass
            _check["filter"] = check
            output.append(_check)
        logger.debug(
            "run_checks [%s] total %.3fs", self.group.name, time.perf_counter() - group_start
        )
        return output

    def run_check_on_user(self, user: User):
        output = []
        group_start = time.perf_counter()
        for check in self.filters.all():
            _filter = check.filter_object
            if _filter is None:
                logger.warning(f"Failed to run filter for {check}")
                continue  # Skip as this is broken...
            try:
                t0 = time.perf_counter()
                test_pass = _filter.audit_filter(
                    User.objects.filter(pk=user.pk)
                )
                logger.debug(
                    "run_check_on_user [%s] filter '%s' took %.3fs",
                    self.group.name, check, time.perf_counter() - t0
                )
            except Exception as e:
                logger.warning("audit_filter failed for '%s', falling back to process_filter: %s", check, e, exc_info=True)
                try:
                    test_pass = {
                        user.id: {
                            "message": "",
                            "check": _filter.process_filter(user)
                        }
                    }
                except Exception as e2:
                    logger.error("process_filter also failed for '%s': %s", check, e2, exc_info=True)
                    test_pass = {
                        user.id: {
                            "message": "Filter Failed",
                            "check": False
                        }
                    }
            _check = {
                "name": check.filter_object.description,
            }
            _check["check"] = test_pass[user.id]['check']
            _check["message"] = test_pass[user.id]['message']
            _check["filter"] = check
            output.append(_check)
        logger.debug(
            "run_check_on_user [%s] total %.3fs", self.group.name, time.perf_counter() - group_start
        )
        return output

    def process_checks(self, checks):
        out = True
        for c in checks:
            out = out & c.get("check", False)
        return out

    def check_user(self, user: User):
        checks = self.run_check_on_user(user)
        out = self.process_checks(checks)
        return out


class GracePeriodRecord(models.Model):
    group = models.ForeignKey(SmartGroup, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    grace_filter = models.ForeignKey(SmartFilter, on_delete=models.CASCADE)
    expires = models.DateTimeField()

    def __str__(self):
        return "{} - {} - {}".format(self.user, self.group, self.grace_filter)

    def is_expired(self):
        return self.expires < timezone.now()

    def notify_user(self, message):
        # dm user if has discord account and discord bot installed
        if app_settings.discord_bot_active():
            try:
                aadiscordbot.tasks.send_direct_message.delay(
                    self.user.discord.uid, message
                )
            except Exception as e:
                logger.error(e, exc_info=True)
                pass
        else:
            pass


from .service_filters import (  # noqa: E402, F401
    DiscordActiveFilter, DiscourseActiveFilter, Ips4ActiveFilter,
    MumbleActiveFilter, OpenfireActiveFilter, Phpbb3ActiveFilter,
    SmfActiveFilter, Teamspeak3ActiveFilter, XenforoActiveFilter,
)


class PendingNotification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    filter = models.ForeignKey(SmartFilter, on_delete=models.CASCADE)
    group = models.ForeignKey(SmartGroup, on_delete=models.CASCADE)

    message = models.TextField(blank=True, default=None, null=True)

    removal = models.BooleanField(default=False)

    notified = models.BooleanField(default=False)

    @classmethod
    def get_grace_notifications(cls):
        notifications = cls.objects.filter(notified=False, removal=False)
        out = defaultdict(list)
        for n in notifications:
            out[n.user].append(n)

        return out, notifications

    @classmethod
    def get_kick_notifications(cls):
        notifications = cls.objects.filter(notified=False, removal=True)
        out = defaultdict(list)
        for n in notifications:
            out[n.user].append(n)

        return out, notifications
