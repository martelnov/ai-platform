BEGIN;

CREATE TABLE IF NOT EXISTS user_mounts (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             bigint NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,

    tree                text NOT NULL
                            CHECK (tree IN ('data', 'models')),

    mount_path          text NOT NULL,

    scope               text NOT NULL
                            CHECK (scope IN ('platform', 'user')),

    editable_by_user    boolean NOT NULL DEFAULT false,

    source_type         text NOT NULL
                            CHECK (source_type IN (
                                'platform',
                                'directory',
                                'block',
                                'smb',
                                'nfs'
                            )),

    source_ref          text NOT NULL,

    filesystem_type     text,
    mount_options       jsonb NOT NULL DEFAULT '{}'::jsonb,
    secret_ref          text,

    encrypted           boolean NOT NULL DEFAULT false,
    encryption_type     text,

    dedup_mode          text NOT NULL DEFAULT 'none'
                            CHECK (dedup_mode IN ('none', 'common')),

    enabled             boolean NOT NULL DEFAULT true,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (user_id, tree, mount_path),

    CHECK (
        mount_path ~ '^/([^/]+(/[^/]+)*)?$'
    ),

    CHECK (
        mount_path !~ '(^|/)(\.|\.\.)(/|$)'
    ),

    CHECK (
        mount_path <> '/'
        OR scope = 'platform'
    ),

    CHECK (
        source_type <> 'platform'
        OR scope = 'platform'
    ),

    CHECK (
        encrypted = true
        OR encryption_type IS NULL
    ),

    CHECK (
        encrypted = false
        OR encryption_type IS NOT NULL
    ),

    CHECK (
        NOT (
            tree = 'models'
            AND scope = 'platform'
            AND encrypted = true
        )
    ),

    CHECK (
        dedup_mode = 'none'
        OR (
            tree = 'models'
            AND scope = 'platform'
            AND encrypted = false
        )
    )
);

CREATE INDEX IF NOT EXISTS user_mounts_user_idx
    ON user_mounts(user_id)
    WHERE enabled = true;


-- Existing users get their two platform roots.

INSERT INTO user_mounts (
    user_id,
    tree,
    mount_path,
    scope,
    editable_by_user,
    source_type,
    source_ref,
    encrypted,
    dedup_mode
)
SELECT
    id,
    'data',
    '/',
    'platform',
    false,
    'platform',
    'bootstrap:data_root',
    false,
    'none'
FROM users
ON CONFLICT (user_id, tree, mount_path) DO NOTHING;


INSERT INTO user_mounts (
    user_id,
    tree,
    mount_path,
    scope,
    editable_by_user,
    source_type,
    source_ref,
    encrypted,
    dedup_mode
)
SELECT
    id,
    'models',
    '/',
    'platform',
    false,
    'platform',
    'bootstrap:models_root',
    false,
    'common'
FROM users
ON CONFLICT (user_id, tree, mount_path) DO NOTHING;

INSERT INTO schema_migrations(version)
VALUES (8);

COMMIT;
