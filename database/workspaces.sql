-- Apply once in the existing Supabase project's SQL editor, then reload the
-- PostgREST schema cache. This adds analytics tables only; existing Auth users
-- and any old banking tables are left intact. No service-role key is needed.
BEGIN;

-- Refuse a name collision instead of changing an unrelated table or its RLS.
-- Only tables created and marked by this migration may be reused on reruns.
DO $$
DECLARE
    analytics_table text;
    relation_oid oid;
    schema_marker constant text := 'mr_analytics_private_workspaces_schema_v1';
BEGIN
    FOREACH analytics_table IN ARRAY ARRAY['public.workspaces', 'public.workspace_messages'] LOOP
        relation_oid := pg_catalog.to_regclass(analytics_table)::oid;
        IF relation_oid IS NOT NULL
           AND pg_catalog.obj_description(relation_oid, 'pg_class') IS DISTINCT FROM schema_marker THEN
            RAISE EXCEPTION 'Refusing to change existing table %: Mr Analytics schema marker is missing or different.', analytics_table
                USING ERRCODE = '55000';
        END IF;
    END LOOP;
END;
$$;

CREATE TABLE IF NOT EXISTS public.workspaces (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    filename text NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
    row_count integer NOT NULL CHECK (row_count BETWEEN 0 AND 100000),
    column_count integer NOT NULL CHECK (column_count BETWEEN 0 AND 100),
    analysis jsonb NOT NULL CHECK (jsonb_typeof(analysis) = 'object' AND octet_length(analysis::text) <= 2097152),
    csv_content text CHECK (csv_content IS NULL OR octet_length(csv_content) <= 10485760),
    state jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(state) = 'object' AND octet_length(state::text) <= 65536),
    notes text NOT NULL DEFAULT '' CHECK (char_length(notes) <= 20000),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    storage_bytes bigint NOT NULL DEFAULT 0 CHECK (storage_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id)
);
COMMENT ON TABLE public.workspaces IS 'mr_analytics_private_workspaces_schema_v1';

CREATE INDEX IF NOT EXISTS workspaces_owner_updated_idx
    ON public.workspaces (owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.workspace_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    workspace_id uuid NOT NULL,
    question text NOT NULL CHECK (char_length(btrim(question)) BETWEEN 1 AND 4000),
    answer text NOT NULL CHECK (char_length(btrim(answer)) BETWEEN 1 AND 20000),
    storage_bytes bigint NOT NULL DEFAULT 0 CHECK (storage_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, owner_id)
        REFERENCES public.workspaces (id, owner_id) ON DELETE CASCADE
);
COMMENT ON TABLE public.workspace_messages IS 'mr_analytics_private_workspaces_schema_v1';

CREATE INDEX IF NOT EXISTS workspace_messages_owner_workspace_idx
    ON public.workspace_messages (owner_id, workspace_id, created_at);

CREATE INDEX IF NOT EXISTS workspace_messages_workspace_owner_idx
    ON public.workspace_messages (workspace_id, owner_id);

-- A per-account transaction lock prevents parallel uploads or saves exceeding
-- the quota. Limits reject the new write; saved datasets are never evicted.
-- SECURITY INVOKER preserves RLS and does not grant elevated data access.
CREATE OR REPLACE FUNCTION public.mr_analytics_check_workspace()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    used_bytes bigint;
    workspace_count integer;
BEGIN
    IF auth.uid() IS NULL OR NEW.owner_id <> auth.uid() THEN
        RAISE EXCEPTION 'workspace_owner_required' USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.owner_id <> OLD.owner_id OR NEW.id <> OLD.id) THEN
        RAISE EXCEPTION 'workspace_owner_immutable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(NEW.owner_id::text, 17339));
    NEW.storage_bytes := pg_catalog.octet_length(NEW.analysis::text)
        + pg_catalog.octet_length(COALESCE(NEW.csv_content, ''))
        + pg_catalog.octet_length(NEW.state::text)
        + pg_catalog.octet_length(NEW.notes)
        + pg_catalog.octet_length(NEW.title)
        + pg_catalog.octet_length(NEW.filename);
    SELECT count(*), COALESCE(sum(storage_bytes), 0)
        INTO workspace_count, used_bytes FROM public.workspaces
        WHERE owner_id = NEW.owner_id AND (TG_OP = 'INSERT' OR id <> NEW.id);
    IF TG_OP = 'INSERT' AND workspace_count >= 25 THEN
        RAISE EXCEPTION 'workspace_count_limit' USING ERRCODE = 'P0001';
    END IF;
    SELECT used_bytes + COALESCE(sum(storage_bytes), 0) INTO used_bytes
        FROM public.workspace_messages WHERE owner_id = NEW.owner_id;
    IF used_bytes + NEW.storage_bytes > 52428800 THEN
        RAISE EXCEPTION 'workspace_storage_limit' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        NEW.revision := OLD.revision + 1;
        NEW.created_at := OLD.created_at;
    ELSE
        NEW.revision := 1;
        NEW.created_at := pg_catalog.clock_timestamp();
    END IF;
    NEW.updated_at := pg_catalog.clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mr_analytics_workspace_quota ON public.workspaces;
CREATE TRIGGER mr_analytics_workspace_quota BEFORE INSERT OR UPDATE ON public.workspaces
    FOR EACH ROW EXECUTE FUNCTION public.mr_analytics_check_workspace();

CREATE OR REPLACE FUNCTION public.mr_analytics_check_message()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    used_bytes bigint;
    message_count integer;
BEGIN
    IF auth.uid() IS NULL OR NEW.owner_id <> auth.uid() THEN
        RAISE EXCEPTION 'workspace_owner_required' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(NEW.owner_id::text, 17339));
    IF NOT EXISTS (SELECT 1 FROM public.workspaces WHERE id = NEW.workspace_id AND owner_id = NEW.owner_id) THEN
        RAISE EXCEPTION 'workspace_owner_required' USING ERRCODE = '42501';
    END IF;
    SELECT count(*) INTO message_count FROM public.workspace_messages
        WHERE workspace_id = NEW.workspace_id AND owner_id = NEW.owner_id;
    IF message_count >= 200 THEN
        RAISE EXCEPTION 'workspace_message_limit' USING ERRCODE = 'P0001';
    END IF;
    NEW.storage_bytes := pg_catalog.octet_length(NEW.question) + pg_catalog.octet_length(NEW.answer);
    SELECT COALESCE(sum(storage_bytes), 0) INTO used_bytes
        FROM public.workspaces WHERE owner_id = NEW.owner_id;
    SELECT used_bytes + COALESCE(sum(storage_bytes), 0) INTO used_bytes
        FROM public.workspace_messages WHERE owner_id = NEW.owner_id;
    IF used_bytes + NEW.storage_bytes > 52428800 THEN
        RAISE EXCEPTION 'workspace_storage_limit' USING ERRCODE = 'P0001';
    END IF;
    NEW.created_at := pg_catalog.clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mr_analytics_message_quota ON public.workspace_messages;
CREATE TRIGGER mr_analytics_message_quota BEFORE INSERT ON public.workspace_messages
    FOR EACH ROW EXECUTE FUNCTION public.mr_analytics_check_message();

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE public.workspace_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspace_messages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mr_analytics_own_workspaces ON public.workspaces;
CREATE POLICY mr_analytics_own_workspaces ON public.workspaces
    FOR ALL TO authenticated
    USING ((SELECT auth.uid()) = owner_id AND (((SELECT auth.jwt())->>'is_anonymous')::boolean IS FALSE))
    WITH CHECK ((SELECT auth.uid()) = owner_id AND (((SELECT auth.jwt())->>'is_anonymous')::boolean IS FALSE));

DROP POLICY IF EXISTS mr_analytics_own_messages ON public.workspace_messages;
CREATE POLICY mr_analytics_own_messages ON public.workspace_messages
    FOR ALL TO authenticated
    USING ((SELECT auth.uid()) = owner_id AND (((SELECT auth.jwt())->>'is_anonymous')::boolean IS FALSE))
    WITH CHECK ((SELECT auth.uid()) = owner_id AND (((SELECT auth.jwt())->>'is_anonymous')::boolean IS FALSE));

-- Supabase anonymous sign-ins also use the authenticated role. Durable
-- customer workspaces require a permanent account; guest reports stay local.
-- Keep reruns compatible with the earlier two-policy setup.
DROP POLICY IF EXISTS mr_analytics_permanent_workspaces ON public.workspaces;
DROP POLICY IF EXISTS mr_analytics_permanent_messages ON public.workspace_messages;

REVOKE ALL ON public.workspaces, public.workspace_messages FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.workspaces TO authenticated;
GRANT SELECT, INSERT, DELETE ON public.workspace_messages TO authenticated;
REVOKE ALL ON FUNCTION public.mr_analytics_check_workspace() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.mr_analytics_check_message() FROM PUBLIC, anon;

NOTIFY pgrst, 'reload schema';
COMMIT;
