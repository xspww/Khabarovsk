"""
Cookie and isolated profile service boundary.

The implementation remains in process_net.IsolationManager during phase 1. This
facade prevents new call sites from depending on the giant legacy module.
"""

from process_net import IsolationManager

__all__ = ["IsolationManager"]

