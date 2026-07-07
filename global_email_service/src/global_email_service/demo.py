"""CLI demo: render or send a sample JSON payload."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import EmailSendError, InvalidPayloadError, load_email_config, send_alert_email
from .table_renderer import render_email_body

EXAMPLES_DIR = Path(__file__).parent / "examples"
DEFAULT_SAMPLE = EXAMPLES_DIR / "sample_cash_all_passed.json"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="global_email_service manual demo")
    parser.add_argument("--file", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.file.read_text(encoding="utf-8"))

    if args.render_only:
        from .service import parse_payload
        request = parse_payload(payload)
        html_body, text_body = render_email_body(
            request.rows, title=request.title, summary=request.summary,
            columns=request.columns, color_overrides=request.color_overrides,
        )
        print("=" * 70)
        print("TEXT VERSION")
        print("=" * 70)
        print(text_body)
        print()
        print("=" * 70)
        print("HTML VERSION")
        print("=" * 70)
        print(html_body)
        return 0

    config = load_email_config()
    if not (config.graph_tenant_id and config.graph_client_id and config.graph_client_secret):
        print("[demo] No Microsoft Graph config found — forcing dry_run=True.\n")
        config.dry_run = True

    try:
        result = send_alert_email(payload, config=config)
    except (InvalidPayloadError, EmailSendError) as exc:
        print(f"[demo] FAILED: {exc}")
        return 1

    print(f"[demo] success={result.success} dry_run={result.dry_run}")
    print(f"[demo] subject={result.subject!r}")
    print(f"[demo] to={result.to} cc={result.cc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
