"""Role B forecasting public entry point (no API or ordering side effects)."""
def build_forecast(snapshot, request):
    """Build using A's shared models, or wire dictionaries before A01 lands."""
    from .service import build_forecast as implementation
    return implementation(snapshot, request)

__all__ = ["build_forecast"]
