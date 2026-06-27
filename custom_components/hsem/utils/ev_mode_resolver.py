"""EV charging mode resolver for negative-price auto-Full override.

Provides :func:`resolve_ev_auto_full` which checks the import price and,
when enabled, promotes EV charging to Full mode during negative-price
periods and restores the previous mode when the price goes positive again.
"""


def resolve_ev_auto_full(
    import_price: float, previous_mode: str | None, enabled: bool
) -> tuple[str | None, str | None]:
    """Check if auto-Full should activate/deactivate based on price.

    Args:
        import_price: Current import electricity price (EUR/kWh).
        previous_mode: The EV charging mode before the auto-Full override
            was applied, or None if no override is active.
        enabled: Whether the auto-Full-on-negative-price feature is enabled
            by the user.

    Returns:
        A tuple ``(effective_mode, restored_from)`` where:

        * ``effective_mode`` is the charging mode to use for the current
          slot (``"full"`` to override, or ``None`` for no override).
        * ``restored_from`` is the previously overridden mode that should
          be recorded (so it can be restored when the price rises above
          zero), or ``None``.
    """
    if not enabled:
        return None, None

    if import_price <= 0.0:
        return "full", previous_mode  # Override to Full

    elif previous_mode is not None:
        return previous_mode, None  # Restore previous mode

    return None, None
