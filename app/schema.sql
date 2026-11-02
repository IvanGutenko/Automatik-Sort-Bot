CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id bigint PRIMARY KEY,title text NOT NULL,enabled boolean NOT NULL DEFAULT true,style text NOT NULL DEFAULT 'autopost');
CREATE TABLE IF NOT EXISTS categories(id serial PRIMARY KEY,code text UNIQUE NOT NULL,title text NOT NULL,description text NOT NULL,department text NOT NULL,destination bigint,link text,enabled boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS products(
 id bigserial PRIMARY KEY,source_channel_id bigint NOT NULL,source_message_id bigint NOT NULL,media_group_id text,
 messages jsonb NOT NULL DEFAULT '[]',closed boolean NOT NULL DEFAULT false,ready_at timestamptz NOT NULL DEFAULT now(),
 status text NOT NULL DEFAULT 'collecting',ai_result jsonb,confidence double precision,ai_category text,final_category text,
 destination_channel_id bigint,destination_message_ids jsonb,review_message_id bigint,review_chat_id bigint,
 error text,attempts int NOT NULL DEFAULT 0,usage jsonb,latency_ms int,estimated_cost_usd numeric,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(source_channel_id,source_message_id));
CREATE INDEX IF NOT EXISTS products_work ON products(status,ready_at);
CREATE TABLE IF NOT EXISTS source_messages(source bigint NOT NULL,message bigint NOT NULL,product_id bigint REFERENCES products(id),PRIMARY KEY(source,message));
CREATE TABLE IF NOT EXISTS updates(id bigint PRIMARY KEY,payload jsonb NOT NULL,done boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS corrections(id bigserial PRIMARY KEY,product_id bigint NOT NULL REFERENCES products(id),ai_category text,final_category text,actor bigint NOT NULL,action text NOT NULL,at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS admin_state(actor bigint PRIMARY KEY,action text NOT NULL,data jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS health(component text PRIMARY KEY,at timestamptz NOT NULL DEFAULT now());

ALTER TABLE sources ADD COLUMN IF NOT EXISTS department text NOT NULL DEFAULT 'unknown';
CREATE TABLE IF NOT EXISTS available_channels(id bigint PRIMARY KEY,title text NOT NULL,status text NOT NULL,can_post boolean NOT NULL DEFAULT false,updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ai_limits(model text PRIMARY KEY,next_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ai_requests(id bigserial PRIMARY KEY,model text NOT NULL,product_id bigint,source_channel_id bigint,started_at timestamptz NOT NULL DEFAULT clock_timestamp(),status_code int,latency_ms int,usage jsonb,error text);
ALTER TABLE products ADD COLUMN IF NOT EXISTS decision_actor bigint;
ALTER TABLE products ADD COLUMN IF NOT EXISTS publish_notice_id bigint;

CREATE TABLE IF NOT EXISTS ai_provider_state(provider text NOT NULL,model text NOT NULL,retry_at double precision NOT NULL DEFAULT 0,last_error text,updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(provider,model));
ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'google';

CREATE TABLE IF NOT EXISTS product_deliveries(
 id bigserial PRIMARY KEY,product_id bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 category_code text NOT NULL,destination bigint NOT NULL,kind text NOT NULL,
 rule_evidence jsonb NOT NULL DEFAULT '{}',source_hash text NOT NULL,source_message_ids jsonb NOT NULL,
 status text NOT NULL DEFAULT 'queued',message_ids jsonb,error text,ready_at timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(product_id,destination));
CREATE INDEX IF NOT EXISTS product_deliveries_product ON product_deliveries(product_id,id);

-- Ordered category decisions; NULL preserves legacy products with one category.
ALTER TABLE products ADD COLUMN IF NOT EXISTS final_categories jsonb;
CREATE TABLE IF NOT EXISTS route_drafts(
 actor bigint NOT NULL,product_id bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 category_ids jsonb NOT NULL DEFAULT '[]',updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(actor,product_id));

-- A category can accept only separately sold garments, regardless of AI extras.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='categories' AND column_name='single_only') THEN
  ALTER TABLE categories ADD COLUMN single_only boolean NOT NULL DEFAULT false;
  UPDATE categories SET single_only=true WHERE code IN ('women.pants','men.pants');
 END IF;
END $$;
