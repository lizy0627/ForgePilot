"""Validate the local Mission model routes without displaying credentials."""

import os

from forgepilot.agents.registry import AgentRegistry
from forgepilot.models.profiles import ModelConfiguration


config = ModelConfiguration.load(os.environ["FORGEPILOT_DEMO_MODEL_CONFIG"])
AgentRegistry(router=config.router())
print("Mission routes valid; reviewer family separation:", config.enforce_review_family_separation)
