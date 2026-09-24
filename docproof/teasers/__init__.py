"""DeepSeek V4 Pro reads the whole manuscript and writes five teasers; Opus 5.5
checks them against the same manuscript and corrects errors with the smallest edit."""

WRITER_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
WRITER_EFFORT = "high"
ADJUDICATOR_MODEL = "claude-opus-5-5"
ADJUDICATOR_EFFORT = "high"
VERSION = 4
AUTHOR_WARNING = "these are made by our staff, you should change them if you wish, talk to your Developmental Editor"
