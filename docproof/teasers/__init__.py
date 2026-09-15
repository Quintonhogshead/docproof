"""Five manuscript-grounded teaser options.

Two models, two jobs that never overlap. Sol (the ChatGPT subscription) reads
the whole manuscript, keeps the ending private, writes a public-safe brief and
judges every draft. An open-weight writer on DeepInfra writes every word an
author will see, from that brief alone. Sol never contributes published text:
no edits, no corrections, no rephrasing. That separation is the point of the
feature — the delivered document is entirely the open-weight model's prose.
"""

SOL_MODEL = "gpt-5.6-sol"
SOL_EFFORT = "high"
# The strongest open-weight writer DocProof's DeepInfra catalog carries. Its
# default reasoning stays on: the writer is doing real editorial work from a
# brief, not paraphrasing finished copy.
WRITER_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
WRITER_PROVIDER = "deepinfra"
VERSION = 2
