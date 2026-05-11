"""
Low-cost process telemetry service.

Phase 1 keeps the implementation in process_net.py for operational safety and
exports the stable boundary here so callers can migrate without a large rewrite.
"""

from process_net import RealtimeResourceMonitor, get_rt_monitor

__all__ = ["RealtimeResourceMonitor", "get_rt_monitor"]

