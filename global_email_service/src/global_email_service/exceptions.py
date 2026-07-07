"""Exceptions raised by the global_email_service module."""

from __future__ import annotations


class EmailServiceError(Exception):
    """Base class for all errors raised by this module."""


class InvalidPayloadError(EmailServiceError):
    """The input JSON payload is missing required fields or malformed."""


class EmailSendError(EmailServiceError):
    """The Microsoft Graph send failed (after any configured retries)."""
