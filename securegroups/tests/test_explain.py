from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import m2m_changed
from django.test import TestCase
from django.urls import reverse

from allianceauth.eveonline.models import (
    EveAllianceInfo, EveCorporationInfo, EveFactionInfo,
)

from .. import models as gb_models, signals as gb_signals


class TestFilterExplain(TestCase):
    @classmethod
    def setUpTestData(cls):
        m2m_changed.disconnect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

        cls.corp = EveCorporationInfo.objects.create(
            corporation_id=601, corporation_name="Explain Corp", corporation_ticker="EXC", member_count=1,
        )
        cls.exempt_corp = EveCorporationInfo.objects.create(
            corporation_id=602, corporation_name="Exempt Corp", corporation_ticker="EMC", member_count=1,
        )
        cls.alliance = EveAllianceInfo.objects.create(
            alliance_id=701, alliance_name="Explain Alliance", alliance_ticker="EXA", executor_corp_id=601,
        )
        cls.faction = EveFactionInfo.objects.create(
            faction_id=801, faction_name="Explain Faction",
        )
        cls.other_group = Group.objects.create(name="Other_Group")

        m2m_changed.connect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

    def test_altcorpfilter_explain_includes_corp_and_exemptions(self):
        f = gb_models.AltCorpFilter.objects.create(
            name="n", description="d", alt_corp=self.corp
        )
        f.exempt_corporations.add(self.exempt_corp)
        html = f.explain()
        self.assertIn("Explain Corp", html)
        self.assertIn("601", html)
        self.assertIn("Exempt Corp", html)
        self.assertIn("exempt", html.lower())

    def test_altcorpfilter_explain_no_exemptions(self):
        f = gb_models.AltCorpFilter.objects.create(
            name="n", description="d", alt_corp=self.corp
        )
        html = f.explain()
        self.assertIn("Explain Corp", html)
        self.assertNotIn("exempt", html.lower())

    def test_altalliancefilter_explain(self):
        f = gb_models.AltAllianceFilter.objects.create(
            name="n", description="d", alt_alli=self.alliance
        )
        html = f.explain()
        self.assertIn("Explain Alliance", html)
        self.assertIn("701", html)

    def test_altfactionfilter_explain(self):
        f = gb_models.AltFactionFilter.objects.create(
            name="n", description="d", alt_faction=self.faction
        )
        html = f.explain()
        self.assertIn("Explain Faction", html)
        self.assertIn("801", html)

    def test_useringroupfilter_explain_normal(self):
        f = gb_models.UserInGroupFilter.objects.create(name="n", description="d")
        f.groups.add(self.other_group)
        html = f.explain()
        self.assertIn("belongs", html)
        self.assertNotIn("NOT", html)
        self.assertIn("Other_Group", html)

    def test_useringroupfilter_explain_reversed(self):
        f = gb_models.UserInGroupFilter.objects.create(
            name="n", description="d", reversed_logic=True
        )
        f.groups.add(self.other_group)
        html = f.explain()
        self.assertIn("does NOT belong", html)

    def test_service_filter_falls_back_to_verbose_name(self):
        from securegroups.service_filters import DiscordActiveFilter
        f = DiscordActiveFilter.objects.create(name="n", description="d")
        html = f.explain()
        self.assertIn("User Has Discord", html)


class TestSmartFilterExplain(TestCase):
    @classmethod
    def setUpTestData(cls):
        m2m_changed.disconnect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

        cls.corp = EveCorporationInfo.objects.create(
            corporation_id=611, corporation_name="Wrap Corp", corporation_ticker="WRC", member_count=1,
        )

        m2m_changed.connect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

    def test_smartfilter_explain_delegates_and_links_to_admin(self):
        f = gb_models.AltCorpFilter.objects.create(
            name="n", description="d", alt_corp=self.corp
        )
        sf = gb_models.SmartFilter.objects.last()
        html = sf.explain()
        self.assertIn("Wrap Corp", html)
        expected_url = reverse("admin:securegroups_altcorpfilter_change", args=[f.pk])
        self.assertIn(expected_url, html)
        self.assertIn("<a href", html)

    def test_smartfilter_explain_handles_broken_reference(self):
        ct = ContentType.objects.get_for_model(gb_models.AltCorpFilter)
        sf = gb_models.SmartFilter.objects.create(
            content_type=ct, object_id=999999, grace_period=5
        )
        html = sf.explain()
        self.assertIn("Broken filter reference", html)


class TestFilterExpressionExplain(TestCase):
    @classmethod
    def setUpTestData(cls):
        m2m_changed.disconnect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

        cls.corp = EveCorporationInfo.objects.create(
            corporation_id=621, corporation_name="Expr Corp A", corporation_ticker="EXA", member_count=1,
        )
        cls.corp2 = EveCorporationInfo.objects.create(
            corporation_id=622, corporation_name="Expr Corp B", corporation_ticker="EXB", member_count=1,
        )

        gb_models.AltCorpFilter.objects.create(name="a", description="a", alt_corp=cls.corp)
        cls.sf1 = gb_models.SmartFilter.objects.last()
        gb_models.AltCorpFilter.objects.create(name="b", description="b", alt_corp=cls.corp2)
        cls.sf2 = gb_models.SmartFilter.objects.last()

        m2m_changed.connect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

    def test_expression_explain_recurses_and_shows_operator(self):
        expr = gb_models.FilterExpression.objects.create(
            name="n", description="d",
            first_term=self.sf1, operator=gb_models.FilterExpression.OperatorChoices.OR,
            second_term=self.sf2,
        )
        html = expr.explain()
        self.assertIn("Expr Corp A", html)
        self.assertIn("Expr Corp B", html)
        self.assertIn("OR", html)
        self.assertNotIn("NOT", html)

    def test_expression_explain_negated(self):
        expr = gb_models.FilterExpression.objects.create(
            name="n", description="d",
            first_term=self.sf1, operator=gb_models.FilterExpression.OperatorChoices.AND,
            second_term=self.sf2, negate_result=True,
        )
        html = expr.explain()
        self.assertIn("NOT", html)
        self.assertIn("AND", html)

    def test_nested_expression_explain(self):
        inner = gb_models.FilterExpression.objects.create(
            name="inner", description="inner",
            first_term=self.sf1, operator=gb_models.FilterExpression.OperatorChoices.OR,
            second_term=self.sf2,
        )
        inner_sf = gb_models.SmartFilter.objects.last()
        outer = gb_models.FilterExpression.objects.create(
            name="outer", description="outer",
            first_term=self.sf1, operator=gb_models.FilterExpression.OperatorChoices.XOR,
            second_term=inner_sf,
        )
        html = outer.explain()
        self.assertIn("XOR", html)
        self.assertIn("OR", html)
        self.assertIn("Expr Corp A", html)
        self.assertIn("Expr Corp B", html)


class TestSmartGroupExplain(TestCase):
    @classmethod
    def setUpTestData(cls):
        m2m_changed.disconnect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

        cls.corp = EveCorporationInfo.objects.create(
            corporation_id=631, corporation_name="Group Explain Corp", corporation_ticker="GEC", member_count=1,
        )
        cls.group = Group.objects.create(name="Explain_Group")
        cls.smart_group = gb_models.SmartGroup.objects.create(
            group=cls.group, auto_group=False, enabled=True
        )

        m2m_changed.connect(gb_signals.m2m_changed_user_groups, sender=User.groups.through)

    def test_explain_no_filters(self):
        html = self.smart_group.explain()
        self.assertIn("No filters configured", html)

    def test_explain_lists_all_filters(self):
        gb_models.AltCorpFilter.objects.create(name="a", description="a", alt_corp=self.corp)
        sf = gb_models.SmartFilter.objects.last()
        self.smart_group.filters.add(sf)
        html = self.smart_group.explain()
        self.assertIn("Group Explain Corp", html)
        self.assertIn("ALL", html)
