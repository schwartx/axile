//! Fixed, read-only raw replay lookup catalog. Callers cannot supply SQL.

pub(super) fn query(operation: &str, count: usize) -> Result<String, &'static str> {
    let sql = match operation {
        "block" => "SELECT id,stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=? AND id=?",
        "block_positions" => "SELECT CASE WHEN length(positions)<=16410 THEN positions END FROM blocks WHERE recording=? AND id=?",
        "block_start" => "SELECT first_seq FROM blocks WHERE recording=? AND id=?",
        "blocks" => "SELECT id,stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=? AND id IN ({ids}) ORDER BY id",
        "business_blocks" => "SELECT id,stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=? AND stream=? AND first_seq>? AND first_seq<=? ORDER BY first_seq LIMIT ?",
        "business_streams" => "SELECT id FROM streams WHERE recording=? AND id>=? AND topic NOT LIKE '/quote/%' AND topic NOT LIKE '/market_trade/%' ORDER BY id LIMIT 129",
        "committed_view" => "SELECT seq,watermark FROM commits WHERE recording=? AND id=?",
        "control_blocks" => "SELECT block FROM scope_blocks WHERE recording=? AND kind=? AND symbol=? AND stream=? AND block>? AND first_seq<=? ORDER BY block LIMIT ?",
        "dependency_origin" => "SELECT 1 FROM relations r JOIN objects o ON o.id=r.target WHERE r.recording=? AND r.source=? AND r.role='related_capture_seq' AND r.seq>? AND r.seq<=? AND o.first_seq<=? LIMIT 1",
        "execution_recording" => "SELECT seq,watermark FROM recordings WHERE id=? AND EXISTS(SELECT 1 FROM executions WHERE recording=? AND id=?)",
        "execution_recordings" => "SELECT r.id,r.seq,r.watermark,r.state,r.commit_id,r.context FROM recordings r JOIN executions e ON e.recording=r.id WHERE e.id=? ORDER BY r.id LIMIT 65",
        "frozen_block" => "SELECT id,stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=? AND id=? AND first_seq<=?",
        "frozen_blocks" => "SELECT id,stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=? AND id IN ({ids}) AND first_seq<=?",
        "generation" => "SELECT value FROM meta WHERE key='generation'",
        "generations" => "SELECT seq,generation FROM generations WHERE recording=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
        "identities" => "SELECT id,kind,key,first_seq FROM objects WHERE recording=? AND kind=? AND (first_seq,id)>(?,?) AND first_seq<=? ORDER BY first_seq,id LIMIT ?",
        "identity_blocks" => "SELECT DISTINCT block FROM object_blocks WHERE recording=? AND object IN ({ids}) AND stream=? AND block>? AND block<=? AND first_seq<=? AND last_seq>? ORDER BY block LIMIT ?",
        "identity_links" => "SELECT o.first_seq AS seq,r.source AS object,EXISTS(SELECT 1 FROM relations x WHERE x.recording=r.recording AND x.target=r.target AND x.role=r.role AND x.source<>r.source AND x.seq<=?) AS conflict FROM relations r JOIN objects o ON o.id=r.target WHERE r.recording=? AND r.source IN ({ids}) AND r.role='related_capture_seq' AND r.seq<=? AND o.first_seq>? AND (o.first_seq,r.source)>(?,?) GROUP BY o.first_seq,r.source ORDER BY o.first_seq,r.source LIMIT 129",
        "identity_plan" => "SELECT first_seq,id FROM objects WHERE recording=? AND kind=? AND (first_seq,id)>(?,?) AND first_seq<=? ORDER BY first_seq,id LIMIT ?",
        "identity_queries" => "SELECT r.seq FROM object_refs r JOIN query_refs q ON q.recording=r.recording AND q.seq=r.seq WHERE r.recording=? AND r.object=? AND r.seq>? AND r.seq<=? ORDER BY r.seq LIMIT ?",
        "identity_streams" => "SELECT DISTINCT stream FROM object_blocks WHERE recording=? AND object IN ({ids}) AND first_seq<=? LIMIT 17",
        "layout" => "SELECT value FROM meta WHERE key='layout'",
        "linked_market_blocks" => "SELECT DISTINCT w.block FROM market_links l JOIN market_objects w ON w.recording=l.recording AND w.first_seq=l.seq AND w.kind=1 WHERE l.recording=? AND l.object IN ({ids}) AND w.stream=? AND w.block>? AND l.seq>? AND l.known_seq<=? ORDER BY w.block LIMIT ?",
        "linked_market_streams" => "SELECT DISTINCT w.stream FROM market_links l JOIN market_objects w ON w.recording=l.recording AND w.first_seq=l.seq AND w.kind=1 WHERE l.recording=? AND l.object IN ({ids}) AND l.known_seq<=? LIMIT 17",
        "location_before" => "SELECT first_seq,last_seq,first_time,last_time,CASE WHEN length(positions)<=3856 THEN positions END AS positions FROM location_pages WHERE recording=? AND first_seq<? ORDER BY first_seq DESC LIMIT 1",
        "location_end" => "SELECT last_seq,last_time FROM location_pages WHERE recording=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
        "location_sequence" => "SELECT first_seq,last_seq,first_time,last_time,CASE WHEN length(positions)<=3856 THEN positions END AS positions FROM location_pages WHERE recording=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
        "location_time" => "SELECT first_seq,last_seq,first_time,last_time,CASE WHEN length(positions)<=3856 THEN positions END AS positions FROM location_pages WHERE recording=? AND (first_time,first_seq)<=(?,?) ORDER BY first_time DESC,first_seq DESC LIMIT 1",
        "market_block" => "SELECT id,stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=? AND id=? AND last_seq>? AND first_seq<=?",
        "market_identities" => "SELECT first_seq,suffix FROM market_objects INDEXED BY market_page WHERE recording=? AND kind=? AND namespace=? AND first_seq>? AND first_seq<=? ORDER BY first_seq LIMIT ?",
        "market_links" => "SELECT l.seq,l.object,EXISTS(SELECT 1 FROM market_links x WHERE x.recording=l.recording AND x.seq=l.seq AND x.object!=l.object AND x.known_seq<=?) AS conflict FROM market_links l WHERE l.recording=? AND l.object IN ({ids}) AND (l.seq,l.object)>(?,?) AND l.seq>? AND l.known_seq<=? ORDER BY l.seq,l.object LIMIT 129",
        "market_namespace" => "SELECT id,prefix FROM market_namespaces WHERE recording=? AND id=?",
        "market_origin" => "SELECT 1 FROM market_links WHERE recording=? AND object IN ({ids}) AND known_seq>? AND known_seq<=? AND seq<=? LIMIT 1",
        "market_recording" => "SELECT id FROM recording_keys WHERE recording=?",
        "market_reference_blocks" => "SELECT block FROM market_refs WHERE recording=? AND object=? AND stream=? AND block>? AND seq>? AND known_seq<=? ORDER BY block LIMIT ?",
        "market_subjects" => "SELECT * FROM market_objects WHERE recording=? AND first_seq IN ({ids}) AND first_seq<=?",
        "next_identity_stream" => "SELECT stream,first_seq FROM object_blocks WHERE recording=? AND object=? AND stream>? ORDER BY stream,block LIMIT 1",
        "next_market_namespace" => "SELECT n.id,n.prefix FROM market_namespaces n WHERE n.recording=? AND n.id>? AND EXISTS(SELECT 1 FROM market_namespace_symbols s WHERE s.namespace=n.id AND s.symbol IN ('',?) AND s.first_seq<=?) ORDER BY n.id LIMIT 1",
        "next_market_stream" => "SELECT stream,known_seq AS first_seq FROM market_refs INDEXED BY market_ref_known WHERE recording=? AND object=? AND stream>? ORDER BY stream,known_seq,block LIMIT 1",
        "next_scope_stream" => "SELECT stream,first_seq FROM scope_blocks WHERE recording=? AND kind=? AND symbol=? AND stream>? ORDER BY stream,block LIMIT 1",
        "order_commands" => "SELECT first_seq,object FROM order_mentions WHERE recording=? AND kind='command' AND order_id=? AND (first_seq,object)>(?,?) AND first_seq<=? ORDER BY first_seq,object LIMIT ?",
        "order_mentions" => "SELECT order_id FROM order_mentions WHERE recording=? AND object=? UNION SELECT m.order_id FROM relations r JOIN order_mentions m ON m.recording=r.recording AND m.object=r.target WHERE r.recording=? AND r.source=? AND r.role='related_capture_seq' AND r.seq<=? ORDER BY order_id LIMIT ?",
        "order_streams" => "SELECT id FROM streams WHERE recording=? AND id IN ({ids}) AND topic IN ('/order','/order_set','/connection')",
        "proof" => "SELECT members FROM proofs WHERE recording=? AND seq=? AND row=?",
        "proof_key" => "SELECT m.position FROM objects o JOIN proof_members m ON m.member=o.id AND m.recording=o.recording WHERE o.recording=? AND o.kind='trade' AND o.key=? AND m.seq=? AND m.row=?",
        "proof_member" => "SELECT position FROM proof_members WHERE recording=? AND seq=? AND row=? AND member=?",
        "quote_block_seed" => "SELECT id,first_seq,last_seq FROM blocks WHERE recording=? AND stream=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
        "quote_block_seed_id" => "SELECT id FROM blocks WHERE recording=? AND stream=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
        "quote_blocks" => "SELECT id FROM blocks WHERE recording=? AND stream=? AND first_seq>=? AND first_seq<=? ORDER BY first_seq LIMIT ?",
        "recording_start" => "SELECT first_time FROM location_pages WHERE recording=? ORDER BY first_seq LIMIT 1",
        "recording_streams" => "SELECT s.id,s.topic,s.symbol FROM streams s WHERE s.recording=? AND (SELECT b.first_seq FROM blocks b WHERE b.recording=s.recording AND b.stream=s.id ORDER BY b.first_seq LIMIT 1)<=? ORDER BY s.id LIMIT 129",
        "recording_symbols" => "SELECT symbol FROM symbols WHERE recording=? AND symbol<>'' AND first_seq<=? ORDER BY symbol LIMIT 129",
        "scope_blocks" => "SELECT block FROM scope_blocks WHERE recording=? AND kind=? AND symbol=? AND stream=? AND block>? AND block<=? AND first_seq<=? AND last_seq>? ORDER BY block LIMIT ?",
        "scope_queries" => "SELECT r.seq FROM scope_refs r JOIN query_refs q ON q.recording=r.recording AND q.seq=r.seq WHERE r.recording=? AND r.kind=? AND r.symbol=? AND r.seq>? AND r.seq<=? ORDER BY r.seq LIMIT ?",
        "stream_block_start" => "SELECT first_seq FROM blocks WHERE recording=? AND id=? AND stream=?",
        "stream_topic" => "SELECT topic FROM streams WHERE recording=? AND id=?",
        "subject_blocks" => "SELECT block FROM object_blocks WHERE recording=? AND object=? AND stream=? AND block>? AND block<=? AND first_seq<=? AND last_seq>? ORDER BY block LIMIT ?",
        "subjects" => "SELECT id,kind,key,first_seq FROM objects NOT INDEXED WHERE recording=? AND id IN ({ids}) AND first_seq<=?",
        "symbol_start" => "SELECT first_seq FROM symbols WHERE recording=? AND symbol=?",
        "topic_streams" => "SELECT id FROM streams WHERE recording=? AND topic=?",
        _ => return Err("REPLAY_INDEX_OPERATION"),
    };
    if sql.contains("{ids}") {
        let width = count
            .checked_sub(sql.matches('?').count())
            .ok_or("REPLAY_INDEX_PARAMETERS")?;
        if !(1..=128).contains(&width) {
            return Err("REPLAY_INDEX_PARAMETERS");
        }
        Ok(sql.replace("{ids}", &vec!["?"; width].join(",")))
    } else if sql.matches('?').count() == count {
        Ok(sql.to_owned())
    } else {
        Err("REPLAY_INDEX_PARAMETERS")
    }
}
