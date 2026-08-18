"""Logging for lumi. Replaces turing-smart-screen-python's library.log logger."""
import logging

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("lumi")
logger.setLevel(logging.INFO)
