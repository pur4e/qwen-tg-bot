import logging
from typing import Optional


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
