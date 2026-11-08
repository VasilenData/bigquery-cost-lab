| Query (table t = crypto_bitcoin.transactions) | Console estimate | On-demand cost |
|---|---|---|
| SELECT * ... LIMIT 10 | 2.38 TB | $14.88 |
| SELECT * EXCEPT(inputs, outputs) ... LIMIT 10 | 347.81 GB | $2.12 |
| two columns, no filter | 101.73 GB | $0.62 |
| one column ... LIMIT 10 | 90.73 GB | $0.55 |
| COUNT(DISTINCT hash) | 90.73 GB | $0.55 |
| APPROX_COUNT_DISTINCT(hash) | 90.73 GB | $0.55 |
| one column, filter block_number > 600000 | 69.59 GB | $0.42 |
| one column, filter EXTRACT(YEAR ...) = 2020 | 7.82 GB | $0.05 |
| one column, filter block_timestamp_month = Jan 2020 | 746.59 MB | under $0.01 |
| one column, filter DATE(block_timestamp) = one day | 85.5 MB | under $0.01 |
| COUNT(*) | 0 B | under $0.01 |
