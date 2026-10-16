"""Sales Dot discovery v2: multi-source company discovery, enrichment, qualification, ranking.

This package never constructs a Gmail client and never sends (tests enforce it). It writes candidates to additive
`disco_*` tables; only promotion (policy-capped) creates CRM prospects + drafts, and the existing
SalesWorker remains the sole sender behind all of its gates.
"""
