-- Flock OS – MariaDB runtime telemetry queries (FLO-49 / FLO-10 §8).
-- Slow-query count (cumulative + delta for rate), live connections, and the
-- InnoDB buffer-pool hit ratio — the D5 revisit trigger for the bulk write path.
--
-- Wrapped by mariadb_metrics.sh; safe to run ad-hoc:
--   mysql -h 127.0.0.1 -u root -p flock_os < load/telemetry/mariadb_metrics.sql

-- Slow queries accumulated since server start (slow_query_log must be ON).
SHOW GLOBAL STATUS LIKE 'Slow_queries';
SHOW GLOBAL STATUS LIKE 'Slow_launch_threads';

-- Connection pressure at 15k-scale concurrency.
SHOW GLOBAL STATUS LIKE 'Threads_connected';
SHOW GLOBAL STATUS LIKE 'Max_used_connections';
SHOW GLOBAL STATUS LIKE 'Aborted_clients';

-- InnoDB buffer-pool hit ratio (D5). INNODB_BUFFER_POOL_STATS.hit_rate is in
-- 1/1000ths of a percent (e.g. 999500 -> 99.95%); a drop below ~99% at scale
-- is the revisit trigger to grow innodb_buffer_pool_size.
SELECT
    pool_id,
    ROUND(hit_rate / 1000, 3) AS buffer_pool_hit_pct,
    database_pages,
    pages_modified
FROM information_schema.INNODB_BUFFER_POOL_STATS;

-- Cross-check hit ratio from the global counters (authoritative):
--   hit_ratio = 1 - (physical_reads / read_requests).
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read_requests';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_reads';
