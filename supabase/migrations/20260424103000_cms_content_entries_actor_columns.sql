DO $$
BEGIN
  IF to_regclass('cms.content_entries') IS NULL THEN
    RAISE NOTICE 'Skipping CMS actor columns migration because cms.content_entries does not exist; run cms-core Drizzle migrations during local bootstrap.';
    RETURN;
  END IF;

  EXECUTE $sql$
    CREATE SCHEMA IF NOT EXISTS cms_migration_backups
  $sql$;

  EXECUTE $sql$
    CREATE TABLE IF NOT EXISTS cms_migration_backups.content_entries_before_actor_columns_20260424 AS
    SELECT *
    FROM cms.content_entries
  $sql$;

  EXECUTE $sql$
    COMMENT ON TABLE cms_migration_backups.content_entries_before_actor_columns_20260424 IS
      'Non-destructive snapshot taken before adding cms.content_entries created_by/updated_by actor columns.'
  $sql$;

  EXECUTE $sql$
    ALTER TABLE cms.content_entries
      ADD COLUMN IF NOT EXISTS created_by uuid
  $sql$;

  EXECUTE $sql$
    ALTER TABLE cms.content_entries
      ADD COLUMN IF NOT EXISTS updated_by uuid
  $sql$;

  IF to_regclass('identity.users') IS NULL THEN
    RAISE NOTICE 'identity.users does not exist; skipping created_by FK constraint';
  ELSIF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'cms.content_entries'::regclass
      AND conname = 'content_entries_created_by_users_id_fk'
  ) THEN
    EXECUTE $sql$
      ALTER TABLE cms.content_entries
        ADD CONSTRAINT content_entries_created_by_users_id_fk
        FOREIGN KEY (created_by) REFERENCES identity.users(id) ON DELETE SET NULL
    $sql$;
  END IF;

  IF to_regclass('identity.users') IS NULL THEN
    RAISE NOTICE 'identity.users does not exist; skipping updated_by FK constraint';
  ELSIF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'cms.content_entries'::regclass
      AND conname = 'content_entries_updated_by_users_id_fk'
  ) THEN
    EXECUTE $sql$
      ALTER TABLE cms.content_entries
        ADD CONSTRAINT content_entries_updated_by_users_id_fk
        FOREIGN KEY (updated_by) REFERENCES identity.users(id) ON DELETE SET NULL
    $sql$;
  END IF;

  IF to_regclass('docs.documents') IS NOT NULL
     AND (
       SELECT count(*) = 2
       FROM information_schema.columns
       WHERE table_schema = 'docs'
         AND table_name = 'documents'
         AND column_name IN ('created_by', 'updated_by')
     )
  THEN
    EXECUTE $sql$
      UPDATE cms.content_entries ce
      SET created_by = d.created_by
      FROM docs.documents d
      WHERE ce.document_id = d.id
        AND ce.created_by IS NULL
        AND d.created_by IS NOT NULL
    $sql$;

    EXECUTE $sql$
      UPDATE cms.content_entries ce
      SET updated_by = COALESCE(d.updated_by, d.created_by)
      FROM docs.documents d
      WHERE ce.document_id = d.id
        AND ce.updated_by IS NULL
        AND COALESCE(d.updated_by, d.created_by) IS NOT NULL
    $sql$;
  END IF;

  EXECUTE $sql$
    CREATE INDEX IF NOT EXISTS content_entries_workspace_created_by_idx
      ON cms.content_entries (workspace_id, created_by)
  $sql$;

  EXECUTE $sql$
    CREATE INDEX IF NOT EXISTS content_entries_workspace_updated_by_idx
      ON cms.content_entries (workspace_id, updated_by)
  $sql$;

  EXECUTE $sql$
    COMMENT ON COLUMN cms.content_entries.created_by IS
      'User that created the CMS entry. Historical rows are nullable when the actor cannot be proven.'
  $sql$;

  EXECUTE $sql$
    COMMENT ON COLUMN cms.content_entries.updated_by IS
      'User that most recently mutated CMS entry metadata/status. Historical rows are nullable when the actor cannot be proven.'
  $sql$;
END $$;
