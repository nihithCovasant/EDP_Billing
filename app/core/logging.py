import logging


def configure_logging(level: int = logging.INFO) -> None:
    """Structured, consistent log format across every module - scheduler,
    worker, clients, API routes all share this formatter."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
