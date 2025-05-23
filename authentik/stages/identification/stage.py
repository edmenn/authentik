"""Identification stage logic"""

from dataclasses import asdict
from random import SystemRandom
from time import sleep
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse
from django.utils.translation import gettext as _
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework.fields import BooleanField, CharField, ChoiceField, DictField, ListField
from rest_framework.serializers import ValidationError
from sentry_sdk import start_span

from authentik.core.api.utils import PassiveSerializer
from authentik.core.models import Application, Source, User
from authentik.events.utils import sanitize_item
from authentik.flows.challenge import (
    Challenge,
    ChallengeResponse,
    RedirectChallenge,
)
from authentik.flows.models import FlowDesignation
from authentik.flows.planner import PLAN_CONTEXT_PENDING_USER
from authentik.flows.stage import PLAN_CONTEXT_PENDING_USER_IDENTIFIER, ChallengeStageView
from authentik.flows.views.executor import SESSION_KEY_APPLICATION_PRE, SESSION_KEY_GET
from authentik.lib.avatars import DEFAULT_AVATAR
from authentik.lib.utils.reflection import all_subclasses
from authentik.lib.utils.urls import reverse_with_qs
from authentik.root.middleware import ClientIPMiddleware
from authentik.stages.captcha.stage import CaptchaChallenge, verify_captcha_token
from authentik.stages.identification.models import IdentificationStage
from authentik.stages.identification.signals import identification_failed
from authentik.stages.password.stage import authenticate


class LoginChallengeMixin:
    """Base login challenge for Identification stage"""


def get_login_serializers():
    mapping = {
        RedirectChallenge().fields["component"].default: RedirectChallenge,
    }
    for cls in all_subclasses(LoginChallengeMixin):
        mapping[cls().fields["component"].default] = cls
    return mapping


@extend_schema_field(
    PolymorphicProxySerializer(
        component_name="LoginChallengeTypes",
        serializers=get_login_serializers,
        resource_type_field_name="component",
    )
)
class ChallengeDictWrapper(DictField):
    """Wrapper around DictField that annotates itself as challenge proxy"""


class LoginSourceSerializer(PassiveSerializer):
    """Serializer for Login buttons of sources"""

    name = CharField()
    icon_url = CharField(required=False, allow_null=True)

    challenge = ChallengeDictWrapper()


class IdentificationChallenge(Challenge):
    """Identification challenges with all UI elements"""

    user_fields = ListField(child=CharField(), allow_empty=True, allow_null=True)
    password_fields = BooleanField()
    allow_show_password = BooleanField(default=False)
    application_pre = CharField(required=False)
    flow_designation = ChoiceField(FlowDesignation.choices)
    captcha_stage = CaptchaChallenge(required=False, allow_null=True)

    enroll_url = CharField(required=False)
    recovery_url = CharField(required=False)
    passwordless_url = CharField(required=False)
    primary_action = CharField()
    sources = LoginSourceSerializer(many=True, required=False)
    show_source_labels = BooleanField()
    enable_remember_me = BooleanField(required=False, default=True)

    component = CharField(default="ak-stage-identification")


class IdentificationChallengeResponse(ChallengeResponse):
    """Identification challenge"""

    uid_field = CharField()
    password = CharField(required=False, allow_blank=True, allow_null=True)
    captcha_token = CharField(required=False, allow_blank=True, allow_null=True)
    component = CharField(default="ak-stage-identification")

    pre_user: User | None = None

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Validate that user exists, and optionally their password and captcha token"""
        uid_field = attrs["uid_field"]
        current_stage: IdentificationStage = self.stage.executor.current_stage
        client_ip = ClientIPMiddleware.get_client_ip(self.stage.request)

        pre_user = self.stage.get_user(uid_field)
        if not pre_user:
            with start_span(
                op="authentik.stages.identification.validate_invalid_wait",
                name="Sleep random time on invalid user identifier",
            ):
                # Sleep a random time (between 90 and 210ms) to "prevent" user enumeration attacks
                sleep(0.030 * SystemRandom().randint(3, 7))
            # Log in a similar format to Event.new(), but we don't want to create an event here
            # as this stage is mostly used by unauthenticated users with very high rate limits
            self.stage.logger.info(
                "invalid_login",
                identifier=uid_field,
                client_ip=client_ip,
                action="invalid_identifier",
                context={
                    "stage": sanitize_item(self.stage),
                },
            )
            identification_failed.send(sender=self, request=self.stage.request, uid_field=uid_field)
            # We set the pending_user even on failure so it's part of the context, even
            # when the input is invalid
            # This is so its part of the current flow plan, and on flow restart can be kept, and
            # policies can be applied.
            self.stage.executor.plan.context[PLAN_CONTEXT_PENDING_USER] = User(
                username=uid_field,
                email=uid_field,
            )
            self.pre_user = self.stage.executor.plan.context[PLAN_CONTEXT_PENDING_USER]
            if not current_stage.show_matched_user:
                self.stage.executor.plan.context[PLAN_CONTEXT_PENDING_USER_IDENTIFIER] = uid_field
            # when `pretend` is enabled, continue regardless
            if current_stage.pretend_user_exists and not current_stage.password_stage:
                return attrs
            raise ValidationError("Failed to authenticate.")
        self.pre_user = pre_user

        # Captcha check
        if captcha_stage := current_stage.captcha_stage:
            captcha_token = attrs.get("captcha_token", None)
            if not captcha_token:
                self.stage.logger.warning("Token not set for captcha attempt")
            verify_captcha_token(captcha_stage, captcha_token, client_ip)

        # Password check
        if not current_stage.password_stage:
            # No password stage select, don't validate the password
            return attrs

        password = attrs.get("password", None)
        if not password:
            self.stage.logger.warning("Password not set for ident+auth attempt")
        try:
            with start_span(
                op="authentik.stages.identification.authenticate",
                name="User authenticate call (combo stage)",
            ):
                user = authenticate(
                    self.stage.request,
                    current_stage.password_stage.backends,
                    current_stage,
                    username=self.pre_user.username,
                    password=password,
                )
            if not user:
                raise ValidationError("Failed to authenticate.")
            self.pre_user = user
        except PermissionDenied as exc:
            raise ValidationError(str(exc)) from exc
        return attrs


class IdentificationStageView(ChallengeStageView):
    """Form to identify the user"""

    response_class = IdentificationChallengeResponse

    def get_user(self, uid_value: str) -> User | None:
        """Find user instance. Returns None if no user was found."""
        current_stage: IdentificationStage = self.executor.current_stage
        query = Q()
        for search_field in current_stage.user_fields:
            model_field = {
                "email": "email",
                "username": "username",
                "upn": "attributes__upn",
            }[search_field]
            if current_stage.case_insensitive_matching:
                model_field += "__iexact"
            else:
                model_field += "__exact"
            query |= Q(**{model_field: uid_value})
        if not query:
            self.logger.debug("Empty user query", query=query)
            return None
        user = User.objects.filter(query).first()
        if user:
            self.logger.debug("Found user", user=user.username, query=query)
            return user
        return None

    def get_primary_action(self) -> str:
        """Get the primary action label for this stage"""
        if self.executor.flow.designation == FlowDesignation.AUTHENTICATION:
            return _("Log in")
        return _("Continue")

    def get_challenge(self) -> Challenge:
        current_stage: IdentificationStage = self.executor.current_stage
        challenge = IdentificationChallenge(
            data={
                "component": "ak-stage-identification",
                "primary_action": self.get_primary_action(),
                "user_fields": current_stage.user_fields,
                "password_fields": bool(current_stage.password_stage),
                "captcha_stage": (
                    {
                        "js_url": current_stage.captcha_stage.js_url,
                        "site_key": current_stage.captcha_stage.public_key,
                        "interactive": current_stage.captcha_stage.interactive,
                        "pending_user": "",
                        "pending_user_avatar": DEFAULT_AVATAR,
                    }
                    if current_stage.captcha_stage
                    else None
                ),
                "allow_show_password": bool(current_stage.password_stage)
                and current_stage.password_stage.allow_show_password,
                "show_source_labels": current_stage.show_source_labels,
                "flow_designation": self.executor.flow.designation,
                "enable_remember_me": current_stage.enable_remember_me,
            }
        )
        # If the user has been redirected to us whilst trying to access an
        # application, SESSION_KEY_APPLICATION_PRE is set in the session
        if SESSION_KEY_APPLICATION_PRE in self.request.session:
            challenge.initial_data["application_pre"] = self.request.session.get(
                SESSION_KEY_APPLICATION_PRE, Application()
            ).name
        get_qs = self.request.session.get(SESSION_KEY_GET, self.request.GET)
        # Check for related enrollment and recovery flow, add URL to view
        if current_stage.enrollment_flow:
            challenge.initial_data["enroll_url"] = reverse_with_qs(
                "authentik_core:if-flow",
                query=get_qs,
                kwargs={"flow_slug": current_stage.enrollment_flow.slug},
            )
        if current_stage.recovery_flow:
            challenge.initial_data["recovery_url"] = reverse_with_qs(
                "authentik_core:if-flow",
                query=get_qs,
                kwargs={"flow_slug": current_stage.recovery_flow.slug},
            )
        if current_stage.passwordless_flow:
            challenge.initial_data["passwordless_url"] = reverse_with_qs(
                "authentik_core:if-flow",
                query=get_qs,
                kwargs={"flow_slug": current_stage.passwordless_flow.slug},
            )

        # Check all enabled source, add them if they have a UI Login button.
        ui_sources = []
        sources: list[Source] = (
            current_stage.sources.filter(enabled=True).order_by("name").select_subclasses()
        )
        for source in sources:
            ui_login_button = source.ui_login_button(self.request)
            if ui_login_button:
                button = asdict(ui_login_button)
                source_challenge = ui_login_button.challenge
                source_challenge.is_valid()
                button["challenge"] = source_challenge.data
                ui_sources.append(button)
        challenge.initial_data["sources"] = ui_sources
        return challenge

    def challenge_valid(self, response: IdentificationChallengeResponse) -> HttpResponse:
        self.executor.plan.context[PLAN_CONTEXT_PENDING_USER] = response.pre_user
        current_stage: IdentificationStage = self.executor.current_stage
        if not current_stage.show_matched_user:
            self.executor.plan.context[PLAN_CONTEXT_PENDING_USER_IDENTIFIER] = (
                response.validated_data.get("uid_field")
            )
        return self.executor.stage_ok()
