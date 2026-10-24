DROP INDEX IF EXISTS uq_intent_account_running;
DROP INDEX IF EXISTS uq_intent_account_queued;
DROP INDEX "ix_execution_intent_account_id_created_at";
DROP INDEX "ix_execution_intent_execution_id";
DROP TABLE "execution_intent";
