"""Synthetic source used by the review-bot showcase."""


CONFIG = {"stage": {"encoder": {"max_batch_size": 8}}}


def encoder_batch_size() -> int:
    return CONFIG["stage"]["encoder"]["max_batch_size"]
