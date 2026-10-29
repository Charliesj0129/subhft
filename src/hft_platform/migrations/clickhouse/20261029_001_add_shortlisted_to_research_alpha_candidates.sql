-- src/hft_platform/migrations/clickhouse/20261029_001_add_shortlisted_to_research_alpha_candidates.sql
-- Research v2 C4: the candidate loop's top-ranked status is SHORTLISTED, not
-- PROMOTED. The loop is a generator: it ranks candidates on its own screening
-- metrics and hands them to the governed Gate A-F path; promotion only happens
-- there. Rows written before this migration keep 'PROMOTED' (append-only, never
-- UPDATE); research/candidate_loop reads both labels as SHORTLISTED.
--
-- Adding an Enum8 value is a metadata-only ALTER: no data is rewritten, and
-- re-running it with the same definition is a no-op. There is no Down: removing
-- a value that rows already hold would fail.
-- Up
ALTER TABLE research.alpha_candidates
    MODIFY COLUMN status Enum8('NEW'=1,'INVALID'=2,'COMPILED'=3,'EVALUATED'=4,
                               'REJECTED'=5,'WATCHLIST'=6,'PROMOTED'=7,'SHORTLISTED'=8);
