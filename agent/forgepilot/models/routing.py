"""Resolve trusted role/use-case routes without granting Agent capabilities."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ..errors import ForgePilotError
from .profiles import ModelConfiguration, ModelProfile


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    primary: ModelProfile
    fallback: ModelProfile | None


def route_uses_cloud(route: ResolvedRoute) -> bool:
    return any(urlsplit(profile.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}
               for profile in (route.primary, route.fallback) if profile)


class ModelRouter:
    def __init__(self, configuration: ModelConfiguration):
        self.configuration = ModelConfiguration.model_validate(configuration)

    def resolve(self, use_case: str, role: str | None = None) -> ResolvedRoute:
        candidates = [route for route in self.configuration.routes
                      if route.route_id == use_case and route.role in (None, role)]
        if not candidates:
            raise ForgePilotError("llm_config", "No model route for this use case and role")
        selected = next((route for route in candidates if role is not None and route.role == role), candidates[0])
        return ResolvedRoute(
            self.configuration.profile(selected.primary),
            self.configuration.profile(selected.fallback) if selected.fallback else None,
        )

    def validate_agent(self, definition) -> ResolvedRoute:
        route = self.resolve(definition.model_route, definition.role)
        if any(not profile.supports_tool_calling for profile in (route.primary, route.fallback) if profile):
            raise ForgePilotError("llm_config", "Agent routes require Tool Calling on every candidate model")
        return route
