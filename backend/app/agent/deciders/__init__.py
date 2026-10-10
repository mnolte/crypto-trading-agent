"""Deciders: the deterministic Rules engine and (later) the OpenAI model.

Both implement the same ``Decider`` protocol and produce the same decision
shape, so the engine and the risk layer never care which one ran.
"""

from app.agent.deciders.base import DecideContext, Decider, Draft
from app.agent.deciders.rules import RulesDecider

__all__ = ["DecideContext", "Decider", "Draft", "RulesDecider"]
