"""Incremental historical futures ingester: Shioaji ticks, Fubon and Shioaji 1-minute K bars.

Writes hft.hist_ticks / hft.hist_kbars / hft.hist_ingest_watermark (migration 20261121_001). Offline research
tooling, off the hot path. Run: python -m research.data_pipeline.hist_ingest --help
"""
