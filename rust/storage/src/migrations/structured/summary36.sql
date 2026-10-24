WITH eligible AS (
    SELECT id FROM performance_snapshot
    WHERE result_schema_version=2 AND cost_schema_version=1
      AND (NULL IS NULL OR id=NULL)
), aggregates AS (
    SELECT t.snapshot_id,t.record_id,COUNT(*) AS trade_count,COUNT(t.value) AS valued,
        SUM(t.value) AS total_value,
        SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.cost END) AS total_cost,
        COUNT(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN 1 END) AS covered,
        SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.value ELSE 0 END) AS covered_value,
        SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.loss_bp*t.value ELSE 0 END) AS weighted_loss,
        SUM(CAST(t.estimated AS INTEGER)) AS estimated,
        COUNT(DISTINCT CASE WHEN t.quantity IS NOT NULL THEN t.symbol END) AS symbol_count,
        COUNT(CASE WHEN t.fee IS NOT NULL AND t.fee_currency IS NOT NULL AND t.fee_currency!=''
                   THEN 1 END) AS fee_covered
    FROM performance_cost_trade AS t JOIN eligible AS s ON s.id=t.snapshot_id
    GROUP BY t.snapshot_id,t.record_id
)
INSERT INTO performance_execution_summary
    (snapshot_id,record_id,time,value,cost,loss_bp,coverage,covered,count,fee_covered,
     amount_complete,covered_value,estimated,symbol_count,has_transactions)
SELECT e.snapshot_id,e.record_id,e.time,a.total_value,a.total_cost,
    CASE WHEN a.covered_value>0 THEN a.weighted_loss/a.covered_value END,
    CASE WHEN a.trade_count=a.valued AND a.total_value>0 THEN a.covered_value/a.total_value END,
    COALESCE(a.covered,0),COALESCE(a.trade_count,0),COALESCE(a.fee_covered,0),
    COALESCE(a.trade_count,0)=COALESCE(a.valued,0),COALESCE(a.covered_value,0),COALESCE(a.estimated,0),
    CASE WHEN e.symbol_count IS NOT NULL THEN COALESCE(a.symbol_count,0) END,
    EXISTS(SELECT 1 FROM performance_cost_transaction AS child
           WHERE child.snapshot_id=e.snapshot_id AND child.record_id=e.record_id)
FROM performance_cost_execution AS e JOIN eligible AS s ON s.id=e.snapshot_id
LEFT JOIN aggregates AS a ON a.snapshot_id=e.snapshot_id AND a.record_id=e.record_id
;
INSERT INTO performance_execution_fee(snapshot_id,record_id,currency,value,count)
SELECT t.snapshot_id,t.record_id,t.fee_currency,SUM(t.fee),COUNT(*)
FROM performance_cost_trade AS t
JOIN performance_execution_summary AS e ON e.snapshot_id=t.snapshot_id AND e.record_id=t.record_id
WHERE t.fee IS NOT NULL AND t.fee_currency IS NOT NULL AND t.fee_currency!=''
  AND (NULL IS NULL OR t.snapshot_id=NULL)
GROUP BY t.snapshot_id,t.record_id,t.fee_currency
;
UPDATE performance_snapshot SET read_view_schema_version=1
WHERE result_schema_version=2 AND cost_schema_version=1
  AND (NULL IS NULL OR id=NULL)
;
