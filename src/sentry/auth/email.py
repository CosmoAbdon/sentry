from __future__ import annotations

import abc
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from sentry.models.organization import Organization
from sentry.models.useremail import UserEmail
from sentry.organizations.services.organization import organization_service
from sentry.users.models.user import User
from sentry.utils import metrics


class AmbiguousUserFromEmail(Exception):
    def __init__(self, email: str, users: Collection[User]) -> None:
        super().__init__(f"Resolved {email!r} to {[user.id for user in users]}")
        self.email = email
        self.users = tuple(users)


def resolve_email_to_user(email: str, organization: Organization | None = None) -> User | None:
    candidates = tuple(UserEmail.objects.filter(email__iexact=email, user__is_active=True))
    if not candidates:
        return None
    return _EmailResolver(email, organization).resolve(candidates)


@dataclass
class _EmailResolver:
    email: str
    organization: Organization | None

    def resolve(self, candidates: Collection[UserEmail]) -> User:
        """Pick the user best matching the email address."""

        if not candidates:
            raise ValueError
        if len(candidates) == 1:
            (unique_email,) = candidates
            return unique_email.user

        for step_cls in self.get_steps():
            step = step_cls(self)
            last_candidates = candidates
            candidates = tuple(step.apply(candidates))
            if len(candidates) == 1:
                # Success: We've narrowed down to only one candidate
                (choice,) = candidates
                step.if_conclusive(last_candidates, choice)
                return choice.user
            if len(candidates) == 0:
                # If the step eliminated all, ignore it and go to the next step
                candidates = last_candidates

        self.if_inconclusive(candidates)
        raise AmbiguousUserFromEmail(self.email, [ue.user for ue in candidates])

    def if_inconclusive(self, remaining_candidates: Collection[UserEmail]) -> None:
        """Hook to call if no step resolves to a single user."""
        metrics.incr("auth.email_resolution.no_resolution", sample_rate=1.0)

    @dataclass
    class ResolutionStep(abc.ABC):
        parent: _EmailResolver

        @abc.abstractmethod
        def apply(self, candidates: Collection[UserEmail]) -> Iterable[UserEmail]:
            raise NotImplementedError

        def if_conclusive(self, candidates: Collection[UserEmail], choice: UserEmail) -> None:
            """Hook to call if this step resolves to a single user."""

    class IsVerified(ResolutionStep):
        """Prefer verified email addresses."""

        def apply(self, candidates: Collection[UserEmail]) -> Iterable[UserEmail]:
            return (ue for ue in candidates if ue.is_verified)

        def if_conclusive(self, candidates: Collection[UserEmail], choice: UserEmail) -> None:
            metrics.incr("auth.email_resolution.by_verification", sample_rate=1.0)

    class HasOrgMembership(ResolutionStep):
        """Prefer users who belong to the organization."""

        def apply(self, candidates: Collection[UserEmail]) -> Iterable[UserEmail]:
            if not self.parent.organization:
                return ()
            candidate_ids = [ue.user_id for ue in candidates]
            member_summaries = organization_service.get_member_summaries_by_ids(
                organization_id=self.parent.organization.id, user_ids=candidate_ids
            )
            users_in_org = {member_summary.user_id for member_summary in member_summaries}
            return (ue for ue in candidates if ue.user_id in users_in_org)

        def if_conclusive(self, candidates: Collection[UserEmail], choice: UserEmail) -> None:
            metrics.incr("auth.email_resolution.by_org_membership", sample_rate=1.0)

    class IsPrimary(ResolutionStep):
        """Prefer users whose primary address matches the address in question."""

        def apply(self, candidates: Collection[UserEmail]) -> Iterable[UserEmail]:
            return (ue for ue in candidates if ue.is_primary())

        def if_conclusive(self, candidates: Collection[UserEmail], choice: UserEmail) -> None:
            metrics.incr("auth.email_resolution.by_primary_email", sample_rate=1.0)

    def get_steps(self) -> Iterable[type[ResolutionStep]]:
        return (
            self.IsVerified,
            self.HasOrgMembership,
            self.IsPrimary,
        )
