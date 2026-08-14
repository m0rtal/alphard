"""Alphard bot entrypoint (Phase 0 stub)."""
import sys
import logging
import time


def main():
    """Main loop stub."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("alphard")

    logger.info("Alphard bot starting (Phase 0 stub)...")
    logger.warning("No agents implemented yet. This is a skeleton.")

    # Health endpoint simple version (Phase 0)
    # TODO: replace with FastAPI app in Phase 1
    try:
        while True:
            logger.info("Heartbeat — agents not yet active")
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
