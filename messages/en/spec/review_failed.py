"""Notifications for the SPEC_REVIEW_FAILED state.

This state is covered by review.notify_failed().
This file exists to explicitly represent the state.
"""

# notify_failed is defined in review.py
# SPEC_REVIEW_FAILED transitions back to SPEC_REVIEW after retry,
# so no dedicated prompt is needed.
