"""Local read-only status protocol without host or platform dependencies."""

from belle_agent.control.protocol import Component, ControlError, Metric, Snapshot

__all__ = ['Component', 'ControlError', 'Metric', 'Snapshot']
