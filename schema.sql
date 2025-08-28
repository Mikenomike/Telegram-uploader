-- schema.sql

-- جدول تنظیمات عمومی
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- کاربران
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  blocked BOOLEAN NOT NULL DEFAULT FALSE,
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- فایل‌ها/لینک‌ها
CREATE TABLE IF NOT EXISTS files (
  id BIGSERIAL PRIMARY KEY,
  storage_chat_id BIGINT NOT NULL,
  storage_message_id BIGINT NOT NULL,
  file_unique_id TEXT NOT NULL,
  file_type TEXT NOT NULL,
  file_size BIGINT NOT NULL DEFAULT 0,
  token TEXT UNIQUE NOT NULL,
  required_channels BIGINT[] NOT NULL DEFAULT '{}',
  active BOOLEAN NOT NULL DEFAULT TRUE,
  views BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- آمار تحویل هر ارسال
CREATE TABLE IF NOT EXISTS deliveries (
  id BIGSERIAL PRIMARY KEY,
  file_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  sent_message_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

-- ایندکس‌ها
CREATE INDEX IF NOT EXISTS idx_files_token ON files (token);
CREATE INDEX IF NOT EXISTS idx_files_created_at ON files (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_file_id ON deliveries (file_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_user_id ON deliveries (user_id);

-- مقدار پیش‌فرض تایمر حذف (۲۰ ثانیه)
INSERT INTO settings(key, value)
VALUES ('delete_timeout_seconds', '20')
ON CONFLICT (key) DO NOTHING;
