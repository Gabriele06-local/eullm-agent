-- The process that runs each run: at startup a process closes only the
-- runs of processes that are gone (their advisory lock is free), never the
-- runs of another process still alive on the same database.
ALTER TABLE core.runs ADD COLUMN instance integer;
